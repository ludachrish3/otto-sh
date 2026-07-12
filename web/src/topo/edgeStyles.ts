// Single source of truth for the topology edge encoding: LinkEdge draws from
// it, TopoLegend renders its swatches from it. A legend that restates these
// styles by hand drifts from the canvas the first time a stroke changes — and
// a wrong key is worse than no key at all.
import type { TopoEdge } from "../data/topology";

export type Provenance = TopoEdge["provenance"];

export interface EdgeStyleSpec {
  /** Legend row text. */
  label: string;
  /** One-line meaning; shown in the hover card. */
  hint: string;
  stroke: string;
  strokeWidth: number;
  strokeDasharray?: string;
  /** Wide, low-opacity sleeve drawn UNDER the core stroke. Tunnels only — no
   * other edge has a casing, so a dynamic link reads as something wrapped
   * *around* a path rather than as a peer of the other links. That is the
   * "topology overlay" Provenance.DYNAMIC's docstring already promises, bought
   * without touching the export contract. The cue is form, not colour: the
   * palette keeps colour reserved for health. */
  casing?: { stroke: string; strokeWidth: number; opacity: number };
}

export const EDGE_STYLES: Record<Provenance, EdgeStyleSpec> = {
  declared: {
    label: "declared",
    hint: "data-plane route from lab.json",
    stroke: "var(--topo-edge-declared)",
    strokeWidth: 2,
  },
  implicit: {
    label: "hop (implicit)",
    hint: "management path derived from hop",
    stroke: "var(--topo-edge-implicit)",
    strokeWidth: 1.5,
  },
  dynamic: {
    label: "tunnel",
    hint: "realized by an otto tunnel",
    stroke: "var(--topo-edge-declared)",
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
  local: {
    label: "from local",
    hint: "directly reachable from this machine",
    stroke: "var(--topo-edge-local)",
    strokeWidth: 1.5,
  },
};

/** Stroke-width delta when an edge is selected or hovered. */
export const EMPHASIS_WIDTH = 1.5;

/** The core stroke, as an inline SVG style object. Strokes are CSS custom
 * properties (app.css), not hex: inline style objects don't participate in
 * Tailwind's `dark:` variant, so the dark alternates flip via the vars'
 * `.dark` overrides instead. */
export function edgeStyle(
  provenance: Provenance,
  emphasized: boolean,
): { stroke: string; strokeWidth: number; strokeDasharray?: string } {
  const spec = EDGE_STYLES[provenance];
  const base = {
    stroke: spec.stroke,
    strokeWidth: spec.strokeWidth,
    ...(spec.strokeDasharray === undefined ? {} : { strokeDasharray: spec.strokeDasharray }),
  };
  return emphasized ? { ...base, strokeWidth: base.strokeWidth + EMPHASIS_WIDTH } : base;
}
