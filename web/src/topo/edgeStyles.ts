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

/** How faint a management edge renders by default. Deliberately NOT zero and
 * NOT hidden behind a toggle: a pale management line crossing behind an
 * element is honest and unobtrusive, and hiding the management plane costs
 * the user real information (design doc §4). This is a product ruling, not a
 * detail. */
const MANAGEMENT_OPACITY = 0.5;

/** `declared` (data-plane) and `dynamic` (tunnel) edges are the network a
 * user came to read; everything else -- `implicit`, `local`, `reports-for`
 * -- is the management star that touches everything and gets faded. This
 * mirrors measure.ts's `classifyEdge` "management" bucket, kept local rather
 * than imported: edgeStyles.ts already keeps its own provenance -> drawing
 * mapping self-contained (see `edgeClass` above), and this is one more axis
 * on that same mapping, not a new module dependency. */
function isManagementProvenance(provenance: Provenance): boolean {
  return provenance !== "declared" && provenance !== "dynamic";
}

/** The core stroke, as an inline SVG style object. Strokes are CSS custom
 * properties (app.css), not hex: inline style objects don't participate in
 * Tailwind's `dark:` variant, so the dark alternates flip via the vars'
 * `.dark-mode` overrides instead.
 *
 * Opacity is a SEPARATE axis from `EdgeClass`, deliberately: `declared` and
 * `implicit`/`local` share one `static` class (identical stroke/width/dash --
 * there is no functional difference in what they draw), but they are NOT the
 * same for this purpose -- `declared` is data-plane and stays full-strength,
 * `implicit`/`local` are management and fade. Folding that into `EDGE_STYLES`
 * would force every `static` edge to fade, taking the data-plane network down
 * with it. Reusing the existing classes and layering opacity on top, rather
 * than inventing a fourth `EdgeClass`, is exactly what keeps that distinction
 * intact. */
export function edgeStyle(
  provenance: Provenance,
  emphasized: boolean,
): { stroke: string; strokeWidth: number; strokeDasharray?: string; opacity?: number } {
  const spec = EDGE_STYLES[edgeClass(provenance)];
  // Emphasis (hover/select) always restores full opacity: a user who has
  // singled out a management edge to inspect it should see it plainly, not
  // squint at a faded one.
  const faint = isManagementProvenance(provenance) && !emphasized;
  const base = {
    stroke: spec.stroke,
    strokeWidth: spec.strokeWidth,
    ...(spec.strokeDasharray === undefined ? {} : { strokeDasharray: spec.strokeDasharray }),
    ...(faint ? { opacity: MANAGEMENT_OPACITY } : {}),
  };
  return emphasized ? { ...base, strokeWidth: base.strokeWidth + EMPHASIS_WIDTH } : base;
}

export type TunnelStatus = "ok" | "degraded" | "uncertain";

/** Ghost opacity for a tunnel whose last scan couldn't reach a hop host. */
const UNCERTAIN_OPACITY = 0.4;

/** Status variants over the ONE tunnel class (spec 2026-07-16 §4): ok = the
 * shipped stroke, degraded = warning accent on identical geometry, uncertain
 * = ghosted. One tunnel, one status — callers apply this to every segment.
 * Colour values exist in BOTH theme blocks (resolve the values, not the
 * vars — the dark-mode-only collision lesson). */
export function tunnelEdgeStyle(
  status: TunnelStatus,
  emphasized: boolean,
): { stroke: string; strokeWidth: number; strokeDasharray?: string; opacity?: number } {
  const base = edgeStyle("dynamic", emphasized);
  if (status === "degraded") return { ...base, stroke: "var(--topo-edge-tunnel-degraded)" };
  if (status === "uncertain" && !emphasized) return { ...base, opacity: UNCERTAIN_OPACITY };
  return base;
}
