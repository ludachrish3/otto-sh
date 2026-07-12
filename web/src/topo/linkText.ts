// Human text for an edge. Shared by the hover card and the inspector so the
// two never disagree about what a link is called.
import type { LinkSnapshot } from "../api/export.gen";
import type { TopoEdge } from "../data/topology";
import { EDGE_STYLES } from "./edgeStyles";

export function endpointText(link: LinkSnapshot): string {
  return link.endpoints
    .map((ep) => {
      const iface = ep.interface ? ` ${ep.interface}` : "";
      const addr = ep.ip ? ` · ${ep.ip}${ep.port != null ? `:${ep.port}` : ""}` : "";
      return `${ep.host}${iface}${addr}`;
    })
    .join("  ⇄  ");
}

function primaryLink(edge: TopoEdge): LinkSnapshot | null {
  return edge.link ?? edge.links?.[0] ?? null;
}

/** Not every edge has a link. `reports-for` never does, and a collapsed hop
 * group carries `links[]` with a synthetic id — showing that id would be
 * noise, so name the pair instead. */
export function edgeTitle(edge: TopoEdge): string {
  if (edge.provenance === "reports-for") return `${edge.source} → ${edge.target}`;
  if (edge.provenance === "local") return `local → ${edge.target}`;
  if (edge.links !== undefined && edge.links.length > 1) return `${edge.source} ⇄ ${edge.target}`;
  const link = primaryLink(edge);
  return link?.name ?? link?.id ?? edge.id;
}

export function edgeSubtitle(edge: TopoEdge): string {
  const label = EDGE_STYLES[edge.provenance].label;
  if (edge.links !== undefined && edge.links.length > 1) {
    return `${label} · ${edge.links.length} links`;
  }
  const protocol = primaryLink(edge)?.protocol;
  return protocol ? `${label} · ${protocol}` : label;
}
