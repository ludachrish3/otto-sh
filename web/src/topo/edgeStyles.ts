// Single source of truth for the topology edge encoding: LinkEdge draws from
// it, TopoLegend renders its swatches from it. A legend that restates these
// styles by hand drifts from the canvas the first time a stroke changes — and
// a wrong key is worse than no key at all.
//
// The canvas encodes CLASSES, not provenances. `declared`, `implicit` and
// `local` links are all derived from lab config — there is no functional
// difference between them — so they share one `static` stroke and the legend
// carries one row for the three. `TopoEdge.provenance` keeps all five values
// and the format:1 export contract is untouched; only the DRAWING collapses.
// The inspector's Provenance row is where the surviving distinction lives.
import type { TopoEdge } from "../data/topology";

export type Provenance = TopoEdge["provenance"];

/** What the canvas actually draws: three classes over five provenances. */
export type EdgeClass = "static" | "tunnel" | "reports-for";

export function edgeClass(provenance: Provenance): EdgeClass {
  if (provenance === "dynamic") return "tunnel";
  if (provenance === "reports-for") return "reports-for";
  return "static"; // declared | implicit | local — all lab config
}

export interface EdgeStyleSpec {
  /** Legend row text. */
  label: string;
  /** One-line meaning; shown in the hover card. */
  hint: string;
  stroke: string;
  strokeWidth: number;
  strokeDasharray?: string;
  /** Wide, low-opacity sleeve drawn UNDER the core stroke. Tunnels only — no
   * other edge has a casing, so a tunnel reads as something wrapped *around* a
   * path rather than as a peer of the other links. That is the "topology
   * overlay" Provenance.DYNAMIC's docstring already promises, bought without
   * touching the export contract. The cue is form, not colour: the palette
   * keeps colour reserved for health. */
  casing?: { stroke: string; strokeWidth: number; opacity: number };
}

export const EDGE_STYLES: Record<EdgeClass, EdgeStyleSpec> = {
  static: {
    label: "static",
    hint: "from the lab config — declared, hop-derived, or local",
    stroke: "var(--topo-edge-static)",
    // 1.5, not declared's old 2: `local` fans out to every hop-less element, so
    // promoting the whole class to 2px would turn the local node into a loud
    // hub. The class takes the lighter weight and the map gets quieter.
    strokeWidth: 1.5,
  },
  tunnel: {
    label: "tunnel",
    hint: "realized by an otto tunnel",
    stroke: "var(--topo-edge-static)",
    // The heaviest stroke on the canvas, deliberately: a tunnel is the only
    // edge that is NOT static lab config.
    strokeWidth: 2,
    strokeDasharray: "7 4",
    casing: { stroke: "var(--topo-edge-tunnel-casing)", strokeWidth: 7, opacity: 0.35 },
  },
  "reports-for": {
    label: "reports for",
    hint: "metrics sourced from a management host",
    stroke: "var(--topo-edge-reports)",
    strokeWidth: 1.5,
    strokeDasharray: "2 5",
  },
};

/** Stroke-width delta when an edge is selected or hovered. */
export const EMPHASIS_WIDTH = 1.5;

/** The core stroke, as an inline SVG style object. Strokes are CSS custom
 * properties (app.css), not hex: inline style objects don't participate in
 * Tailwind's `dark:` variant, so the dark alternates flip via the vars'
 * `.dark-mode` overrides instead. */
export function edgeStyle(
  provenance: Provenance,
  emphasized: boolean,
): { stroke: string; strokeWidth: number; strokeDasharray?: string } {
  const spec = EDGE_STYLES[edgeClass(provenance)];
  const base = {
    stroke: spec.stroke,
    strokeWidth: spec.strokeWidth,
    ...(spec.strokeDasharray === undefined ? {} : { strokeDasharray: spec.strokeDasharray }),
  };
  return emphasized ? { ...base, strokeWidth: base.strokeWidth + EMPHASIS_WIDTH } : base;
}
