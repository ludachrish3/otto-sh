// Deterministic layered layout: column -> x, stable row order -> y. No force
// physics: positions never jitter, so screenshots and Playwright assertions
// stay reproducible. O(n log n); fine for lab scale (tens of nodes).
import { isManagementElement, type TopoEdge, type TopoNode } from "../data/topology";

/** Column pitch. The gutter it leaves (COL_W minus the 208px element node) has
 * to hold TWO things at once: a bow wide enough to carry a same-column edge
 * clear of the boxes between its endpoints, AND enough spread between parallel
 * edges that each one keeps its own pointer target. React Flow gives every edge
 * a 20px-wide invisible hit path, so two parallel edges whose centrelines are
 * less than ~10px apart share a single hit target — the inner one becomes
 * unclickable, its inspector unreachable.
 *
 * At 280 the gutter was 72px, which fits the bow but leaves only ~4px of spread:
 * `app-db` was literally unreachable under `metrics-udp` (0 of 19 sampled points
 * on its own stroke resolved back to it). 320 leaves 112px, which carries both,
 * and holds up to FOUR parallel links between one pair. See routing.ts. */
export const COL_W = 320;
export const ROW_H = 110;

function rowOrder(a: TopoNode, b: TopoNode): number {
  if ((a.kind === "local") !== (b.kind === "local")) return a.kind === "local" ? -1 : 1;
  const slotA = a.host?.slot ?? Number.POSITIVE_INFINITY;
  const slotB = b.host?.slot ?? Number.POSITIVE_INFINITY;
  return slotA - slotB || a.id.localeCompare(b.id);
}

const isDataPlaneEdge = (e: TopoEdge): boolean => e.provenance === "declared";

interface DataPlaneBackbone {
  /** Node ids surviving leaf-peel -- the layered backbone. */
  remaining: Set<string>;
  /** DATA-PLANE (declared-only) adjacency over every non-management node,
   * INCLUDING peeled ones (a peeled node just isn't in `remaining` any more)
   * -- callers that need backbone-only traversal must gate with
   * `remaining.has`. */
  adjacency: Map<string, Set<string>>;
  /** Resolves a peeled leaf id to its surviving backbone anchor, walking a
   * dock chain transitively (a leaf's attachment can itself get peeled in a
   * later round -- e.g. sprawl's zephyr-01->tor-sw-b->tor-sw-a->app-01). Also
   * safe to call on a backbone id (returns it unchanged, no-op). */
  resolveDock: (id: string) => string;
}

/** Rule 1+2: subtract management (Task 3's `isManagementElement`), then
 * iteratively peel data-plane (`declared`-only) degree-1 elements out of the
 * backbone and record where each got docked.
 *
 * GUARD: a degree-1 node is only peeled if its neighbour's OWN remaining
 * degree is >= 2 -- otherwise a length-1 island (two elements linked only to
 * each other, nothing else -- e.g. sprawl's `edge-gw <-> core-gw`) would
 * strip itself to nothing with no surviving anchor to dock against. That
 * mutual pair is left in the backbone instead, as its own tiny component.
 *
 * Degree-2 nodes are NEVER peeled by this rule, on purpose -- ring peers
 * (isp-core's `acc-*`, each with one `agg` uplink and one ring link to a
 * sibling `acc-*`) are a real tier, not a decoration; peeling them collapses
 * that tier into the hub's column and regresses toward the pre-redesign
 * hairball (measured, see the design doc). Only true degree-1 pendants (a
 * service hanging off ONE attachment, e.g. isp-core's `hss-01`) get folded
 * away. */
function peelDataPlaneBackbone(
  nodes: TopoNode[],
  edges: TopoEdge[],
  managementIds: Set<string>,
): DataPlaneBackbone {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const adjacency = new Map<string, Set<string>>();
  const addAdj = (a: string, b: string): void => {
    let set = adjacency.get(a);
    if (!set) {
      set = new Set();
      adjacency.set(a, set);
    }
    set.add(b);
  };
  for (const e of edges) {
    if (!isDataPlaneEdge(e)) continue;
    const a = byId.get(e.source);
    const b = byId.get(e.target);
    if (!a || !b) continue;
    if (isManagementElement(a, managementIds) || isManagementElement(b, managementIds)) continue;
    addAdj(e.source, e.target);
    addAdj(e.target, e.source);
  }

  const remaining = new Set(
    nodes.filter((n) => !isManagementElement(n, managementIds)).map((n) => n.id),
  );
  const remainingDegree = (id: string): number => {
    let d = 0;
    for (const nb of adjacency.get(id) ?? []) if (remaining.has(nb)) d++;
    return d;
  };

  // Iterative, guarded leaf-peel. `peeledTo` records the attachment AT TIME
  // OF PEEL -- resolved transitively to a surviving anchor below, since that
  // attachment can itself be peeled in a LATER round (a peel exposing a new
  // degree-1 node one hop further in).
  const peeledTo = new Map<string, string>();
  for (;;) {
    const toPeel: Array<[string, string]> = [];
    for (const id of remaining) {
      if (remainingDegree(id) !== 1) continue;
      const nb = [...(adjacency.get(id) ?? [])].find((x) => remaining.has(x));
      if (nb === undefined) continue;
      if (remainingDegree(nb) >= 2) toPeel.push([id, nb]);
      // else: mutual pendant pair (or `nb` already committed to peel this
      // same round) -- neither side peeled, both stay in the backbone.
    }
    if (toPeel.length === 0) break;
    for (const [id, nb] of toPeel) {
      peeledTo.set(id, nb);
      remaining.delete(id);
    }
  }
  const resolveDock = (id: string): string => {
    let cur = id;
    const seen = new Set<string>();
    while (peeledTo.has(cur) && !seen.has(cur)) {
      seen.add(cur);
      cur = peeledTo.get(cur) as string;
    }
    return cur;
  };

  return { remaining, adjacency, resolveDock };
}

function median(values: number[]): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
}

/** Rule 4's confidence guard. A component's branches are only ORIENTED
 * (split into upstream/downstream by subtree mass) when the spread between
 * the largest and smallest branch is DECISIVE -- the largest branch has to
 * be at least `DECISIVE_RATIO` times the smallest. Below that, every branch
 * defaults to the SAME direction (downstream), i.e. plain unsigned
 * hop-distance from the root cluster -- "symmetric" layering, the rule
 * declining to act on a signal it does not have.
 *
 * **Load-bearing for isp-core; INERT for sprawl -- verified, not assumed.**
 * isp-core's real branch sizes are [1, 2, 3, 3, 3, 3] (ratio 3): forcing
 * `DECISIVE_RATIO = 4` (above that ratio) breaks the `pe-*`-upstream
 * invariant, so the guard genuinely decides isp-core's orientation. Sprawl's
 * hub branches are tied at [3, 3] (ratio 1) -- but sprawl is NOT protected
 * by failing the decisiveness check. It is protected by `branchSign` below:
 * a branch only flips upstream when its size is STRICTLY LESS THAN the
 * branch median, and an exact tie sits AT the median, never below it, so a
 * tied branch never orients regardless of what `decisive` evaluates to.
 * Forcing `DECISIVE_RATIO = 1` (which makes sprawl's tied split register as
 * "decisive") produces BYTE-IDENTICAL sprawl output -- `3 < 3` is false
 * either way. Do not credit this guard with sprawl's stability; that credit
 * belongs to the strict `<` in `branchSign`. (An earlier draft of this
 * comment, and the design doc, both made that false claim -- corrected in
 * Task 7 review.)
 *
 * This is a deliberately round, non-fixture-tuned number, not a value fit to
 * either measured graph -- picked for what it means (a 2:1 split is
 * unambiguously asymmetric), not walked to a value that makes a fixture
 * pass. See dataPlaneColumns's docstring for the measured crossing counts
 * this produces. */
const DECISIVE_RATIO = 2;

/** Rules 3+4: per connected component of the post-peel backbone, root on the
 * spine CLUSTER -- every node whose degree is >= 75% of that component's own
 * maximum degree (a true k-core decomposition was tried and rejected: it
 * degenerates to "coreness 2 for everyone" on both fixtures, no signal at
 * all) -- so mutually-linked spine peers (isp-core's `core-01`/`core-02`)
 * land in the SAME layer instead of one hop apart. Then, ONLY when decisive
 * (see DECISIVE_RATIO), orient each remaining branch by its own subtree mass:
 * heavy (>= the component's median branch size) goes downstream (positive
 * layer, hop-distance from the root cluster); light goes upstream (negative
 * layer). Each component's layers are normalised to start at 0 independently
 * -- components don't share a root, so their column-index spaces legitimately
 * overlap (column N means "N hops from THIS component's own root", not a
 * single global root). */
function layerBackbone(
  remaining: Set<string>,
  adjacency: Map<string, Set<string>>,
): Map<string, number> {
  const components: string[][] = [];
  const seen = new Set<string>();
  for (const start of remaining) {
    if (seen.has(start)) continue;
    const comp: string[] = [];
    const queue = [start];
    seen.add(start);
    while (queue.length > 0) {
      const cur = queue.shift() as string;
      comp.push(cur);
      for (const nb of adjacency.get(cur) ?? []) {
        if (!remaining.has(nb) || seen.has(nb)) continue;
        seen.add(nb);
        queue.push(nb);
      }
    }
    components.push(comp);
  }

  const rawLayer = new Map<string, number>();
  for (const comp of components) {
    const compSet = new Set(comp);
    const degree = (id: string): number => {
      let d = 0;
      for (const nb of adjacency.get(id) ?? []) if (compSet.has(nb)) d++;
      return d;
    };
    const maxDeg = Math.max(...comp.map(degree));
    const root = new Set(comp.filter((id) => degree(id) >= 0.75 * maxDeg));

    // Branches = connected components of (comp \ root). Two root-adjacent
    // neighbours that are ALSO connected to each other once the root is
    // removed (isp-core's pe-01<->pgw-01) land in the SAME branch -- they
    // are one structure, not independent alternatives.
    const branchOf = new Map<string, number>();
    const branchSizes: number[] = [];
    const branchSeen = new Set<string>();
    for (const id of comp) {
      if (root.has(id) || branchSeen.has(id)) continue;
      const branchIdx = branchSizes.length;
      const stack = [id];
      branchSeen.add(id);
      let size = 0;
      while (stack.length > 0) {
        const cur = stack.pop() as string;
        branchOf.set(cur, branchIdx);
        size++;
        for (const nb of adjacency.get(cur) ?? []) {
          if (!compSet.has(nb) || root.has(nb) || branchSeen.has(nb)) continue;
          branchSeen.add(nb);
          stack.push(nb);
        }
      }
      branchSizes.push(size);
    }

    // The confidence guard: with fewer than two branches there is nothing to
    // compare (single branch, or the whole component IS the root), so it
    // falls through to the same "everyone downstream" path as an indecisive
    // split -- there's no tie to break because there's no second side.
    const minSize = branchSizes.length > 0 ? Math.min(...branchSizes) : 0;
    const maxSize = branchSizes.length > 0 ? Math.max(...branchSizes) : 0;
    const decisive = branchSizes.length >= 2 && minSize > 0 && maxSize >= DECISIVE_RATIO * minSize;
    const branchMedian = median(branchSizes);
    // sign[i] = -1 (upstream) only when the split is decisive AND this
    // branch is strictly below the median; otherwise every branch is +1
    // (downstream) -- both the "not decisive" fallback and the "at/above
    // median" case share this default. This strict `<` is sprawl's REAL
    // protection against the equal-mass tie collapsing the map: an exact
    // tie sits AT the median, so it never satisfies `size < branchMedian`
    // and never orients, whether or not `decisive` is true. See
    // DECISIVE_RATIO's docstring -- the guard does not do this work.
    const branchSign = branchSizes.map((size) => (decisive && size < branchMedian ? -1 : 1));

    // Multi-source BFS from the root cluster over the FULL component (not
    // comp\root) for hop distance; sign comes from the node's branch.
    const dist = new Map<string, number>();
    const bfsQueue: string[] = [];
    for (const id of root) {
      dist.set(id, 0);
      bfsQueue.push(id);
    }
    while (bfsQueue.length > 0) {
      const cur = bfsQueue.shift() as string;
      const d = dist.get(cur) as number;
      for (const nb of adjacency.get(cur) ?? []) {
        if (!compSet.has(nb) || dist.has(nb)) continue;
        dist.set(nb, d + 1);
        bfsQueue.push(nb);
      }
    }

    for (const id of comp) {
      if (root.has(id)) {
        rawLayer.set(id, 0);
        continue;
      }
      const b = branchOf.get(id);
      const sign = b !== undefined ? branchSign[b] : 1;
      rawLayer.set(id, sign * (dist.get(id) ?? 1));
    }

    // Normalise THIS component's layers to start at 0.
    const compLayers = comp.map((id) => rawLayer.get(id) as number);
    const minLayer = Math.min(...compLayers);
    for (const id of comp) rawLayer.set(id, (rawLayer.get(id) as number) - minLayer);
  }

  return rawLayer;
}

/** The column every node lays out in, replacing the old `x = depth * COL_W`
 * (hops along the MANAGEMENT chain -- meaningless for the data plane once
 * management paths are short, which collapsed a 23-element real lab into 3
 * columns with 75 element-link crossings; see the design doc). The pipeline,
 * in order:
 *
 * 1. Subtract management (Task 3's `isManagementElement` / `managementIds`)
 *    -- management nodes get column 0, their own leftmost band, regardless
 *    of the data-plane structure below.
 * 2. Peel leaf services and dock them into their attachment's column
 *    (`peelDataPlaneBackbone`).
 * 3. Root the remaining backbone on its spine CLUSTER, per connected
 *    component.
 * 4. Orient each branch by subtree mass -- only when decisive; symmetric
 *    (unsigned, all-downstream) layering otherwise (`layerBackbone`).
 *
 * Row order within a column and y-coordinates are NOT this function's
 * concern -- `layoutTopo` still does today's slot/id row order and top-down
 * `row * ROW_H` placement; the barycentric row-sort and centred coordinate
 * assignment the design doc also calls for are later phases.
 *
 * Measured (chromium/firefox/webkit, identical; `dp_crossings`/`dp_swallowed`,
 * data-plane edges only), THIS function's column assignment combined with
 * `layoutTopo`'s unchanged `rowOrder`+top-aligned grid (Tasks 5/6's
 * barycentric row-sort and centred coordinate assignment are NOT yet
 * applied, and would reduce these further -- the reference prototype's own
 * numbers for the full pipeline, 12/3, are not reachable by this function
 * alone): isp-core 75->15 crossings / 0->0 swallowed; sprawl 21->13
 * crossings / 3->0 swallowed. See task-4-report.md for the full before/after
 * and the confidence-guard decision. */
export function dataPlaneColumns(
  nodes: TopoNode[],
  edges: TopoEdge[],
  managementIds: Set<string>,
): Map<string, number> {
  const { remaining, adjacency, resolveDock } = peelDataPlaneBackbone(nodes, edges, managementIds);
  const rawLayer = layerBackbone(remaining, adjacency);

  const columnOf = new Map<string, number>();
  for (const n of nodes) {
    if (isManagementElement(n, managementIds)) {
      columnOf.set(n.id, 0);
      continue;
    }
    if (rawLayer.has(n.id)) {
      // Backbone node: normalised layer + 1, reserving column 0 for
      // management.
      columnOf.set(n.id, (rawLayer.get(n.id) as number) + 1);
      continue;
    }
    // Peeled leaf: dock into its (transitively resolved) attachment's
    // column, not a column of its own.
    const anchor = resolveDock(n.id);
    columnOf.set(n.id, (rawLayer.get(anchor) ?? 0) + 1);
  }
  return columnOf;
}

const isDataPlaneOnly = (e: TopoEdge): boolean => e.provenance === "declared";

/** Builds, once, the adjacent-column (|colDiff| === 1) DATA-PLANE neighbour
 * lists both `barycentricRowSort` and `coordinateAssignment` need: nodeId ->
 * (neighbour's column -> neighbour ids in that column). A skip-column edge
 * gets no vote on either endpoint's row -- the standard Sugiyama
 * restriction, matching the reference prototype. */
function adjacentColumnNeighbors(
  edges: TopoEdge[],
  columnOfId: Map<string, number>,
): Map<string, Map<number, string[]>> {
  const neighborsAcross = new Map<string, Map<number, string[]>>();
  const addNeighbor = (a: string, b: string, colB: number): void => {
    let m = neighborsAcross.get(a);
    if (!m) {
      m = new Map();
      neighborsAcross.set(a, m);
    }
    const arr = m.get(colB);
    if (arr) arr.push(b);
    else m.set(colB, [b]);
  };
  for (const e of edges) {
    if (!isDataPlaneOnly(e)) continue;
    const ca = columnOfId.get(e.source);
    const cb = columnOfId.get(e.target);
    if (ca === undefined || cb === undefined || Math.abs(ca - cb) !== 1) continue;
    addNeighbor(e.source, e.target, cb);
    addNeighbor(e.target, e.source, ca);
  }
  return neighborsAcross;
}

/** Rule 5: barycentric row-sort, on DATA-PLANE links only. Today `rowOrder`
 * sorts a column alphabetically (slot then id), so linked peers can land far
 * apart and their edges bow across the column -- see the acceptance case in
 * topolayout.test.ts (`app-01`/`tor-sw-a`, same column, 5 rows apart under
 * plain alphabetical order). Management links (`local:*`, `implicit` hop
 * edges, `reports-for`) must NOT drag row order around -- the management
 * plane is an overlay, not the skeleton, and letting it vote would defeat
 * the whole point of Task 4's redesign.
 *
 * Ported from the reference prototype's `barycentricRowSort`
 * (`.../scratchpad/layout-prototype/preview/variants.ts`), scoped to
 * `declared`-only edges for BOTH the barycentre sweep and the final
 * same-column adjacency bias (the prototype's W5/W6 usage, not V3's
 * every-edge scope). Mutates `byColumn`'s arrays in place; `byColumn` must
 * already hold each column's INITIAL order (the caller's `rowOrder` sort) --
 * that is the "initialise from the current order" the design doc asks for.
 *
 * Four alternating sweeps (left-to-right, then right-to-left): each column
 * is re-sorted by the MEAN row-index of its data-plane neighbours in the
 * already-updated adjacent column (Gauss-Seidel). A node with no such
 * neighbour keeps its previous relative position (stable sort -- "no
 * signal" never reorders). A final pass nudges same-column DATA-PLANE-linked
 * pairs to be strictly adjacent. */
function barycentricRowSort(byColumn: Map<number, TopoNode[]>, edges: TopoEdge[]): void {
  const columns = [...byColumn.keys()].sort((a, b) => a - b);
  const columnOfId = new Map<string, number>();
  for (const [col, bucket] of byColumn) for (const n of bucket) columnOfId.set(n.id, col);
  const neighborsAcross = adjacentColumnNeighbors(edges, columnOfId);

  const rowIndexOf = (bucket: TopoNode[]): Map<string, number> => {
    const m = new Map<string, number>();
    bucket.forEach((n, i) => {
      m.set(n.id, i);
    });
    return m;
  };
  const barycenter = (
    nodeId: string,
    refCol: number,
    refIndex: Map<string, number>,
  ): number | null => {
    const neigh = neighborsAcross.get(nodeId)?.get(refCol);
    if (!neigh || neigh.length === 0) return null;
    const idxs = neigh.map((id) => refIndex.get(id)).filter((v): v is number => v !== undefined);
    if (idxs.length === 0) return null;
    return idxs.reduce((a, b) => a + b, 0) / idxs.length;
  };
  const sweepColumn = (col: number, refCol: number): void => {
    const bucket = byColumn.get(col);
    const ref = byColumn.get(refCol);
    if (!bucket || !ref) return;
    const refIndex = rowIndexOf(ref);
    const withKey = bucket.map((n, i) => ({ n, i, bc: barycenter(n.id, refCol, refIndex) }));
    withKey.sort((a, b) => {
      if (a.bc === null && b.bc === null) return a.i - b.i; // no signal: stable
      if (a.bc === null) return 1;
      if (b.bc === null) return -1;
      return a.bc - b.bc || a.i - b.i;
    });
    byColumn.set(
      col,
      withKey.map((w) => w.n),
    );
  };

  for (let sweep = 0; sweep < 4; sweep++) {
    if (sweep % 2 === 0) {
      for (const c of columns) sweepColumn(c, c - 1); // left -> right
    } else {
      for (let i = columns.length - 1; i >= 0; i--) sweepColumn(columns[i], columns[i] + 1); // right -> left
    }
  }

  // Final bias: within a column, pull data-plane-LINKED nodes adjacent.
  // Repeated to a fixpoint (bounded) rather than run once: a single pass is
  // ORDER-DEPENDENT -- moving a later edge's target to sit beside its source
  // can silently undo an earlier edge's adjacency (measured on sprawl:
  // `db-01<->db-02` lands adjacent first, then the later `workers<->db-01`
  // pair -- a docked leaf sharing db-01's column -- shoves db-01 next to
  // `workers` instead, breaking `db-01`/`db-02` apart). A second pass over
  // the SAME rule resolves both at once (db-01 settles BETWEEN db-02 and
  // workers, satisfying both adjacencies simultaneously) -- this is not a
  // different algorithm, just the identical single-pass rule given enough
  // passes to reach a stable arrangement, matching the "a few sweeps"
  // discipline the barycentre phase above already uses. Bounded at 3 passes
  // (a full extra sweep beyond the 2 empirically needed here) to guard
  // against pathological oscillation in a greedy splice-based reordering.
  const linkEdges = edges.filter(isDataPlaneOnly);
  for (let pass = 0; pass < 3; pass++) {
    let moved = false;
    for (const col of columns) {
      const bucket = byColumn.get(col);
      if (!bucket) continue;
      const sameColEdges = linkEdges.filter(
        (e) => columnOfId.get(e.source) === col && columnOfId.get(e.target) === col,
      );
      for (const e of sameColEdges) {
        const order = bucket.map((n) => n.id);
        const ia = order.indexOf(e.source);
        const ib = order.indexOf(e.target);
        if (ia === -1 || ib === -1 || Math.abs(ia - ib) <= 1) continue;
        const [entry] = bucket.splice(ib, 1);
        const newIa = bucket.findIndex((n) => n.id === e.source);
        bucket.splice(newIa + 1, 0, entry);
        moved = true;
      }
    }
    if (!moved) break;
  }
}

/** Rule 6: coordinate assignment -- the Sugiyama phase this layout simply
 * never had. Row ORDER (`barycentricRowSort`) decides who sits above whom;
 * this decides WHERE, in continuous y. Until now `y = row * ROW_H`, so every
 * column was TOP-ALIGNED at y = 0 -- which is why `local` sat in the top-left
 * corner with its edges raking down-and-right instead of radiating from the
 * middle of the map.
 *
 * Ported from the reference prototype's `coordinateAssignment`
 * (`.../scratchpad/layout-prototype/preview/variants.ts`), a Brandes-Kopf-lite
 * pass, with the prototype's own hard-won constraint kept intact:
 *
 * **Row ORDER is an INPUT here and is never re-sorted.** Real Brandes-Kopf
 * deliberately does not reorder within a layer -- the crossing-minimised order
 * comes from the previous phase, and coordinate assignment only adjusts
 * continuous y while PRESERVING it. The prototype measured what happens if you
 * re-sort each column by target y every sweep: per-node numeric drift silently
 * permutes the order across the Gauss-Seidel rounds and `dp_crossings` comes
 * out WORSE than the row-sort it started from. So `bucket`'s order is fixed,
 * and values are only ever pushed forward to keep the minimum gap.
 *
 * Per sweep (4, alternating direction): each node's target y is the MEDIAN y
 * of its DATA-PLANE neighbours in the already-updated adjacent column (median,
 * not mean -- the design doc asks for the median, and it is the more robust
 * centre when a hub has one far-flung neighbour; measured, the two produce
 * identical crossing counts on both fixtures, so this follows the spec).
 *
 * A node with no data-plane neighbour in the ADJACENT column falls back to the
 * median y of its SAME-COLUMN data-plane neighbours. Without that fallback a
 * DOCKED LEAF is stranded: `hss-01`'s only declared link is to `core-02`, which
 * Task 4 docks into `hss-01`'s own column, so it has no cross-column
 * neighbour at all, gets no target, and sits at its stale grid y while every
 * node around it is pulled toward its own neighbours -- measured, that left
 * `hss-01` at y=0 with `core-02` at y=1320, 12 rows from the node it hangs off,
 * its link bowing the entire height of the column. That is precisely the
 * "linked peers sit far apart and their links bow" failure this redesign
 * exists to remove, so a leaf with no cross-column opinion follows the node it
 * is docked to. (The reference prototype has no such fallback; it keeps the
 * node's current y. It never showed up there because its docked leaves happened
 * to sort adjacent on the uniform grid it started from.)
 *
 * A node with NEITHER kind of data-plane neighbour keeps its current y -- every
 * management node carries zero declared edges, so it has no data-plane opinion
 * about where it belongs and does not move on its own. Overlaps are then
 * resolved by pushing forward to a minimum gap of ROW_H, preserving order.
 *
 * Finally every column's vertical extent is centred on the tallest column's
 * midpoint, replacing top-alignment. This is what lifts the management column
 * (short, and with zero data-plane pull, so it never moves on its own) off the
 * top-left corner and into the vertical middle. */
function coordinateAssignment(
  byColumn: Map<number, TopoNode[]>,
  edges: TopoEdge[],
  baseY: Map<string, number>,
): Map<string, number> {
  const columns = [...byColumn.keys()].sort((a, b) => a - b);
  const columnOfId = new Map<string, number>();
  for (const [col, bucket] of byColumn) for (const n of bucket) columnOfId.set(n.id, col);
  const neighborsAcross = adjacentColumnNeighbors(edges, columnOfId);

  // Same-column data-plane neighbours -- the dock relationship a leaf like
  // `hss-01` has with `core-02`. Only consulted when a node has no
  // cross-column neighbour to take a target from.
  const sameColumnNeighbors = new Map<string, string[]>();
  for (const e of edges) {
    if (!isDataPlaneOnly(e)) continue;
    const ca = columnOfId.get(e.source);
    const cb = columnOfId.get(e.target);
    if (ca === undefined || cb === undefined || ca !== cb) continue;
    const push = (a: string, b: string): void => {
      const arr = sameColumnNeighbors.get(a);
      if (arr) arr.push(b);
      else sameColumnNeighbors.set(a, [b]);
    };
    push(e.source, e.target);
    push(e.target, e.source);
  }

  const y = new Map(baseY);

  const refineColumn = (col: number, refCol: number): void => {
    const bucket = byColumn.get(col);
    if (!bucket || !byColumn.has(refCol)) return;
    // Stage 1: every node that HAS a cross-column neighbour takes the median of
    // those neighbours' y. Nodes without one are left undecided.
    const crossTarget = new Map<string, number>();
    for (const n of bucket) {
      const neigh = neighborsAcross.get(n.id)?.get(refCol);
      if (neigh && neigh.length > 0) {
        crossTarget.set(n.id, median(neigh.map((id) => y.get(id) ?? 0)));
      }
    }
    // Stage 2: an undecided node (a docked leaf, e.g. `hss-01`) follows the
    // median of its same-column data-plane neighbours' STAGE-1 TARGETS, not
    // their current y -- chasing the anchor's stale position just re-opens the
    // gap every sweep, since the anchor moves to its own target in the very
    // same pass.
    const targets = bucket.map((n) => {
      const own = crossTarget.get(n.id);
      if (own !== undefined) return own;
      const docked = sameColumnNeighbors.get(n.id);
      if (docked && docked.length > 0) {
        const anchored = docked
          .map((id) => crossTarget.get(id) ?? y.get(id))
          .filter((v): v is number => v !== undefined);
        if (anchored.length > 0) return median(anchored);
      }
      return y.get(n.id) ?? 0;
    });
    // Order-preserving: walk the column in its (fixed) row order and push each
    // node down to at least ROW_H below the previous one.
    let prevY = Number.NEGATIVE_INFINITY;
    for (let i = 0; i < bucket.length; i++) {
      const ny = Math.max(targets[i], prevY + ROW_H);
      y.set(bucket[i].id, ny);
      prevY = ny;
    }
  };

  for (let sweep = 0; sweep < 4; sweep++) {
    if (sweep % 2 === 0) {
      for (const c of columns) refineColumn(c, c - 1); // left -> right
    } else {
      for (let i = columns.length - 1; i >= 0; i--) refineColumn(columns[i], columns[i] + 1); // right -> left
    }
  }

  // Centre every column on the tallest column's midpoint, replacing the old
  // top-alignment (every column starting at y = 0).
  let tallestExtent = 0;
  for (const bucket of byColumn.values()) {
    if (bucket.length === 0) continue;
    const ys = bucket.map((n) => y.get(n.id) ?? 0);
    tallestExtent = Math.max(tallestExtent, Math.max(...ys) - Math.min(...ys));
  }
  const referenceMid = tallestExtent / 2;
  for (const bucket of byColumn.values()) {
    if (bucket.length === 0) continue;
    const ys = bucket.map((n) => y.get(n.id) ?? 0);
    const shift = referenceMid - (Math.min(...ys) + Math.max(...ys)) / 2;
    for (const n of bucket) y.set(n.id, (y.get(n.id) ?? 0) + shift);
  }

  return y;
}

export function layoutTopo(
  nodes: TopoNode[],
  edges: TopoEdge[],
  managementIds: Set<string>,
): Map<string, { x: number; y: number }> {
  const columns = dataPlaneColumns(nodes, edges, managementIds);
  const byColumn = new Map<number, TopoNode[]>();
  for (const n of nodes) {
    const col = columns.get(n.id) ?? 0;
    const bucket = byColumn.get(col);
    if (bucket) bucket.push(n);
    else byColumn.set(col, [n]);
  }
  for (const bucket of byColumn.values()) bucket.sort(rowOrder);
  barycentricRowSort(byColumn, edges);

  // The row-sorted grid is coordinate assignment's STARTING point (its row
  // order is the input it must preserve); it then replaces the rigid
  // `row * ROW_H` placement with a neighbour-derived, centred one.
  const baseY = new Map<string, number>();
  for (const bucket of byColumn.values()) {
    bucket.forEach((n, row) => {
      baseY.set(n.id, row * ROW_H);
    });
  }
  const y = coordinateAssignment(byColumn, edges, baseY);

  const out = new Map<string, { x: number; y: number }>();
  for (const [col, bucket] of byColumn) {
    for (const n of bucket) {
      out.set(n.id, { x: col * COL_W, y: y.get(n.id) ?? 0 });
    }
  }
  return out;
}
