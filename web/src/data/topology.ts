// Topology graph model (spec 2026-07-11): reachability cascade + node/edge
// derivation. Pure. The layout redesign (docs/superpowers/plans/2026-07-14-
// topology-layout-redesign.md) inverted which structure places a node:
// `layoutTopo` (topo/layout.ts) now derives every node's COLUMN from the
// DATA-PLANE (`declared`-link) graph alone -- the hop skeleton places
// nothing. `TopoNode.depth` is still computed here (walking the hop chain
// via `chainOf`/`depthOf`), but is write-only in production; see its own
// doc comment for why it survives. Hop cycles (misconfig) are guarded,
// clamped and warned — never an infinite loop -- independently of depth,
// via `deriveReachability`'s own walk of every host's hop chain.
import type { HostSnapshot, LinkSnapshot } from "../api/export.gen";
import type { DerivedElement, NormalizedSession } from "./exportDoc";
import type { SubjectHealth } from "./health";

export type EffectiveStatus = SubjectHealth["status"] | "unreachable";

export interface ReachabilityResult {
  effective: Map<string, EffectiveStatus>;
  warnings: string[];
}

export interface TopoNode {
  id: string;
  kind: "local" | "element" | "host";
  /** Hop-chain depth from `local` (`local` = 0, +1 per hop toward it) --
   * WRITE-ONLY in production now: `layoutTopo` (topo/layout.ts) no longer
   * takes it, having switched to columning by DATA-PLANE (`declared`-link)
   * structure instead (see this file's header comment). Kept, rather than
   * deleted, for two reasons: computing it (`depthOf` below) walks each
   * host's `hop` chain via `chainOf`, which is what surfaces hop-cycle
   * misconfiguration warnings into this graph's own `warnings` (a walk
   * `deriveReachability` also performs independently, over every host,
   * regardless of view -- so this one is currently redundant belt-and-
   * braces, not the sole source, but the two are separate call sites, and
   * dropping this one is a separate change from dropping the field); and
   * `topology.test.ts` asserts exact depth values as a pin on that walk.
   * Untangling "keep the warning walk, stop storing the number" would touch
   * every TopoNode literal the tests construct by hand -- left for a
   * follow-up, not folded into this fix. */
  depth: number;
  label: string;
  element?: DerivedElement;
  host?: HostSnapshot;
  effective?: EffectiveStatus;
  rollup?: EffectiveStatus[];
  enterTarget?: string;
}

export interface TopoEdge {
  id: string;
  source: string;
  target: string;
  provenance: "implicit" | "declared" | "dynamic" | "local" | "reports-for";
  link?: LinkSnapshot;
  links?: LinkSnapshot[];
  impair: string | null;
  parallelIndex: number;
}

export interface TopoGraph {
  nodes: TopoNode[];
  edges: TopoEdge[];
  warnings: string[];
  /** Element ids inferred as management infrastructure -- see
   * `deriveManagementIds`. Computed once from the session, independent of
   * `opts` (identical whether `expand`/`sources` are set or not), so
   * `layoutTopo` and callers don't have to re-derive it. */
  managementIds: Set<string>;
}

interface ChainResult {
  ancestors: HostSnapshot[];
  cyclic: boolean;
  cycleAt: string | null;
}

/** Walk a host's hop chain toward local. Cycle-guarded, dangling-tolerant. */
function chainOf(host: HostSnapshot, byId: Map<string, HostSnapshot>): ChainResult {
  const seen = new Set<string>([host.id]);
  const ancestors: HostSnapshot[] = [];
  let cursor = host.hop ?? null;
  while (cursor != null) {
    if (seen.has(cursor)) return { ancestors, cyclic: true, cycleAt: cursor };
    seen.add(cursor);
    const ancestor = byId.get(cursor);
    if (!ancestor) break; // dangling hop id: treat as attached here
    ancestors.push(ancestor);
    cursor = ancestor.hop ?? null;
  }
  return { ancestors, cyclic: false, cycleAt: null };
}

export function deriveReachability(
  session: NormalizedSession,
  healths: Map<string, SubjectHealth>,
): ReachabilityResult {
  const byId = new Map(session.lab.hosts.map((h) => [h.id, h]));
  const effective = new Map<string, EffectiveStatus>();
  const warnings: string[] = [];
  for (const host of session.lab.hosts) {
    const own: EffectiveStatus = healths.get(host.id)?.status ?? "unknown";
    const chain = chainOf(host, byId);
    if (chain.cyclic) {
      warnings.push(`hop cycle at "${chain.cycleAt}" (walking from ${host.id})`);
      effective.set(host.id, "unknown");
      continue;
    }
    // A host still reporting is reachable by definition; only silent hosts
    // are reinterpreted by a dead ancestor.
    const silent = own === "down" || own === "no-data";
    const deadAncestor = chain.ancestors.some((a) => healths.get(a.id)?.status === "down");
    effective.set(host.id, silent && deadAncestor ? "unreachable" : own);
  }
  return { effective, warnings };
}

/** The key for an UNORDERED pair of nodes: sorted, so `pairKey(a, b)` and
 * `pairKey(b, a)` agree. Parallel-edge grouping depends on that. */
export function pairKey(a: string, b: string): string {
  return a < b ? `${a}~${b}` : `${b}~${a}`;
}

/** Element ids inferred as MANAGEMENT infrastructure, computed from the
 * SESSION alone -- never from the rendered graph. This must give the
 * IDENTICAL set regardless of the Sources toggle, because whether an
 * element is management is a property of the lab, not of which view
 * options happen to be on.
 *
 * An element qualifies when it has zero `declared` (data-plane) links in
 * `session.lab.links` -- "one declared link" is not "zero declared links",
 * so a network element keeps its data-plane status however few links it
 * has -- AND at least one of:
 *
 * - it is something another host routes THROUGH: some host's `hop` field
 *   resolves to a host that is a member of this element; or
 * - it is something that REPORTS: it is the metric `source` for a host
 *   that belongs to a DIFFERENT element.
 *
 * The naive version of this reads those two facts off the rendered
 * `TopoEdge[]` instead -- `hop` edges and `reports-for` edges. That is
 * wrong: `reports-for` edges are only rendered when `opts.sources` is on,
 * and `TopologyPage`'s Sources toggle defaults OFF, so an EMS-like element
 * would silently un-manage itself in the default view. Reading `hop` and
 * `source`/`host` off the session directly sidesteps that -- both are
 * present whether or not `sources` is on.
 *
 * An explicit `management`/`tier` field is a later phase that merely
 * overrides this inference -- it does not replace it. */
export function deriveManagementIds(session: NormalizedSession): Set<string> {
  const elementOf = new Map<string, string>();
  for (const el of session.elements) {
    for (const id of el.hostIds) elementOf.set(id, el.id);
  }

  const declared = new Set<string>();
  for (const link of session.lab.links) {
    if ((link.provenance ?? "declared") !== "declared") continue;
    for (const ep of link.endpoints) {
      const el = elementOf.get(ep.host);
      if (el) declared.add(el);
    }
  }

  const hopTargets = new Set<string>(); // host ids some other host routes through
  for (const host of session.lab.hosts) {
    if (host.hop != null) hopTargets.add(host.hop);
  }

  const reportSources = new Set<string>(); // host ids reporting for a DIFFERENT element
  for (const m of session.metrics) {
    if (m.source == null || m.host == null) continue;
    const srcEl = elementOf.get(m.source);
    const hostEl = elementOf.get(m.host);
    if (srcEl != null && hostEl != null && srcEl !== hostEl) reportSources.add(m.source);
  }

  const ids = new Set<string>();
  for (const el of session.elements) {
    if (declared.has(el.id)) continue; // >=1 declared link: data-plane, full stop
    const isHopTarget = el.hostIds.some((id) => hopTargets.has(id));
    const isReportSource = el.hostIds.some((id) => reportSources.has(id));
    if (isHopTarget || isReportSource) ids.add(el.id);
  }
  return ids;
}

/** True for the `local` node, and for any element/host node whose OWNING
 * element is in `managementIds` (see `deriveManagementIds`). A `host`-kind
 * node (intra-element view) resolves through its own `host.element`, since
 * `managementIds` is always an ELEMENT-id set -- management-ness is a
 * property of the element, not of the view level it's rendered at. */
export function isManagementElement(node: TopoNode, managementIds: Set<string>): boolean {
  if (node.kind === "local") return true;
  const elementId = node.kind === "host" ? (node.host?.element ?? node.id) : node.id;
  return managementIds.has(elementId);
}

function assignParallelIndices(edges: TopoEdge[]): void {
  const groups = new Map<string, TopoEdge[]>();
  for (const e of edges) {
    const key = pairKey(e.source, e.target);
    const list = groups.get(key);
    if (list) list.push(e);
    else groups.set(key, [e]);
  }
  for (const list of groups.values()) {
    list.sort((a, b) => a.id.localeCompare(b.id));
    list.forEach((e, i) => {
      e.parallelIndex = i;
    });
  }
}

function slotThenId(byId: Map<string, HostSnapshot>) {
  return (a: string, b: string): number => {
    const slotA = byId.get(a)?.slot ?? Number.POSITIVE_INFINITY;
    const slotB = byId.get(b)?.slot ?? Number.POSITIVE_INFINITY;
    return slotA - slotB || a.localeCompare(b);
  };
}

export function buildTopoGraph(
  session: NormalizedSession,
  effective: Map<string, EffectiveStatus>,
  opts: { expand?: string; sources: boolean },
): TopoGraph {
  const byId = new Map(session.lab.hosts.map((h) => [h.id, h]));
  const warnings: string[] = [];
  const elementOf = new Map<string, string>();
  for (const el of session.elements) {
    for (const id of el.hostIds) elementOf.set(id, el.id);
  }
  const depthOf = (host: HostSnapshot): number => {
    const chain = chainOf(host, byId);
    if (chain.cyclic) warnings.push(`hop cycle at "${chain.cycleAt}" (walking from ${host.id})`);
    return chain.ancestors.length + 1;
  };
  // Deduplicate cycle warnings (reachability already reported them per host).
  const uniq = (list: string[]): string[] => [...new Set(list)];

  const nodes: TopoNode[] = [{ id: "local", kind: "local", depth: 0, label: "local" }];
  const edges: TopoEdge[] = [];
  const statusOf = (id: string): EffectiveStatus => effective.get(id) ?? "unknown";

  // Distinct (source host, fed host) pairs from metric attribution.
  const feeds = new Map<string, Set<string>>();
  if (opts.sources) {
    for (const m of session.metrics) {
      if (m.source == null || m.host == null) continue;
      if (!byId.has(m.source) || !byId.has(m.host)) continue;
      let set = feeds.get(m.source);
      if (!set) {
        set = new Set();
        feeds.set(m.source, set);
      }
      set.add(m.host);
    }
  }

  if (opts.expand === undefined) {
    // ── Inter-element graph ──────────────────────────────────────────────
    for (const el of session.elements) {
      const members = [...el.hostIds].sort(slotThenId(byId));
      const memberHosts = members
        .map((id) => byId.get(id))
        .filter((h): h is HostSnapshot => h !== undefined);
      const depth = memberHosts.length ? Math.min(...memberHosts.map((h) => depthOf(h))) : 1;
      nodes.push({
        id: el.id,
        kind: "element",
        depth,
        label: el.id,
        element: el,
        rollup: members.map(statusOf),
        enterTarget: el.singleton ? `/host/${members[0]}` : `/topology/${el.id}`,
      });
      if (memberHosts.some((h) => h.hop == null)) {
        edges.push({
          id: `local:${el.id}`,
          source: "local",
          target: el.id,
          provenance: "local",
          impair: null,
          parallelIndex: 0,
        });
      }
    }
    const nodeOf = (hostId: string): string | undefined => elementOf.get(hostId);
    const implicitByPair = new Map<string, { a: string; b: string; links: LinkSnapshot[] }>();
    for (const link of session.lab.links) {
      const a = nodeOf(link.endpoints[0].host);
      const b = nodeOf(link.endpoints[1].host);
      if (!a || !b || a === b) continue; // dangling or intra-element: not at this level
      if ((link.provenance ?? "declared") === "implicit") {
        const key = pairKey(a, b);
        const group = implicitByPair.get(key);
        if (group) group.links.push(link);
        else implicitByPair.set(key, { a, b, links: [link] });
      } else {
        edges.push({
          id: link.id,
          source: a,
          target: b,
          provenance: link.provenance === "dynamic" ? "dynamic" : "declared",
          link,
          impair: link.impair ?? null,
          parallelIndex: 0,
        });
      }
    }
    for (const [key, group] of implicitByPair) {
      edges.push({
        id: `implicit:${key}`,
        source: group.a,
        target: group.b,
        provenance: "implicit",
        links: group.links,
        impair: null,
        parallelIndex: 0,
      });
    }
    for (const [src, fed] of feeds) {
      const srcNode = nodeOf(src);
      if (!srcNode) continue;
      const targets = new Set([...fed].map((h) => nodeOf(h)).filter((t) => t && t !== srcNode));
      for (const target of targets) {
        edges.push({
          id: `reports:${src}~${target}`,
          source: srcNode,
          target: target as string,
          provenance: "reports-for",
          impair: null,
          parallelIndex: 0,
        });
      }
    }
  } else {
    // ── Intra-element graph ──────────────────────────────────────────────
    const el = session.elements.find((e) => e.id === opts.expand);
    const members = new Set(el?.hostIds ?? []);
    const include = new Map<string, HostSnapshot>();
    const addHost = (h: HostSnapshot | undefined): void => {
      if (h && !include.has(h.id)) include.set(h.id, h);
    };
    for (const id of members) {
      const host = byId.get(id);
      addHost(host);
      if (host) for (const ancestor of chainOf(host, byId).ancestors) addHost(ancestor);
    }
    const rendered = session.lab.links.filter(
      (l) => members.has(l.endpoints[0].host) || members.has(l.endpoints[1].host),
    );
    for (const link of rendered) {
      addHost(byId.get(link.endpoints[0].host));
      addHost(byId.get(link.endpoints[1].host));
    }
    if (opts.sources) {
      for (const [src, fed] of feeds) {
        if ([...fed].some((h) => members.has(h))) addHost(byId.get(src));
      }
    }
    const ordered = [...include.values()].sort((a, b) => slotThenId(byId)(a.id, b.id));
    for (const host of ordered) {
      nodes.push({
        id: host.id,
        kind: "host",
        depth: depthOf(host),
        label: host.id,
        host,
        effective: statusOf(host.id),
        enterTarget: `/host/${host.id}`,
      });
      if (host.hop == null) {
        edges.push({
          id: `local:${host.id}`,
          source: "local",
          target: host.id,
          provenance: "local",
          impair: null,
          parallelIndex: 0,
        });
      } else if (include.has(host.hop)) {
        edges.push({
          id: `hop:${host.id}`,
          source: host.hop,
          target: host.id,
          provenance: "implicit",
          impair: null,
          parallelIndex: 0,
        });
      }
    }
    for (const link of rendered) {
      const [a, b] = [link.endpoints[0].host, link.endpoints[1].host];
      if (!include.has(a) || !include.has(b)) continue;
      if ((link.provenance ?? "declared") === "implicit") continue; // hop edges already drawn
      edges.push({
        id: link.id,
        source: a,
        target: b,
        provenance: link.provenance === "dynamic" ? "dynamic" : "declared",
        link,
        impair: link.impair ?? null,
        parallelIndex: 0,
      });
    }
    if (opts.sources) {
      for (const [src, fed] of feeds) {
        for (const h of fed) {
          if (!members.has(h) || !include.has(src)) continue;
          edges.push({
            id: `reports:${src}~${h}`,
            source: src,
            target: h,
            provenance: "reports-for",
            impair: null,
            parallelIndex: 0,
          });
        }
      }
    }
  }

  assignParallelIndices(edges);
  return { nodes, edges, warnings: uniq(warnings), managementIds: deriveManagementIds(session) };
}
