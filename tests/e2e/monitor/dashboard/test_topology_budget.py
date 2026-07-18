"""The layout BUDGET: measure the topology map instead of judging it by eye.

Two numbers decide whether the map is readable:

- ``dp_crossings`` -- crossings where BOTH edges are data-plane.
- ``dp_swallowed`` -- a data-plane edge whose path passes UNDER a
  non-endpoint node. React Flow draws edges beneath nodes, so a swallowed
  edge is invisible to the user.

Only DATA-PLANE edges (``provenance: "declared"`` -- element<->element
network links) count. Management edges (``local:*``, hop-derived
``implicit:*``, ``reports-for``) may freely cross behind an element -- a
faint management line passing behind a node is honest and unobtrusive, not
clutter -- and are deliberately excluded. An earlier round of this
investigation counted them anyway and it inverted the ranking of every
candidate layout (see ``docs/superpowers/plans/2026-07-14-topology-layout-
redesign.md``).

This budget runs HERE, in the browser lane, not in vitest, because
``routeEdge`` (``web/src/topo/routing.ts``) routes against React Flow's
*measured* node rects -- a unit test would have to assume node heights, and
assumed geometry certifies the wrong artifact (the same class of mistake as
testing against a stale bundle -- see issues #131/#132 and this directory's
own ``conftest.py`` staleness guard). ``web/src/topo/measure.ts`` and its
unit tests (``web/src/__tests__/topomeasure.test.ts``) cover the pure
sampling/classification MATH; this module re-implements the same three rules
(classify by provenance, "swallowed" = a sampled point inside a non-endpoint
node's rect, "crossing" = a segment intersection between two sampled
polylines) as an in-page ``page.evaluate`` script, because the compiled app
bundle has nothing to import from -- there is no ``window`` debug hook
wiring ``measure.ts`` into the shipped page, so the rules are the shared
contract, not the module object.

Budgets are pinned to TODAY's measured baseline and assert ``<=``, so later
layout work ratchets them down (Task 7) rather than rewriting this test.
"""

from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
    pytest.mark.xdist_group("dashboard"),
]

FIXTURES = Path(__file__).resolve().parents[4] / "web" / "fixtures"

# Sample density for both the "swallowed" containment check and crossing
# detection -- matches measure.ts's own CROSSING_SAMPLES constant and the
# `samples` argument its own unit tests pass to `countSwallowed`.
SAMPLES = 40

# Small margin on crossings only: engine-to-engine font-metric differences
# can nudge a node's measured width/height by a pixel or two, which can flip
# a near-miss crossing. Containment ("swallowed") is a much coarser test (a
# sampled point landing inside or outside a whole node rect), so it gets no
# margin -- baselines were measured at 0/0 (Task 4) and should not drift.
CROSSING_MARGIN = 2

# Baselines measured against TODAY's layout. Task 3 partitioned management
# out (column 0, faint edges); Task 4 replaced the data-plane x-axis with
# `dataPlaneColumns` (subtract management -> peel+dock leaf services -> root
# on the spine cluster -> orient by subtree mass, only when decisive --
# see layout.ts). Task 5 added the barycentric row-sort on data-plane links
# only, replacing alphabetical row order within a column: 15 -> 4 on isp-core,
# 13 -> 6 on sprawl. Task 6 added coordinate assignment -- y from the median y
# of a node's data-plane neighbours, overlaps pushed apart to a ROW_H gap, and
# every column CENTRED instead of top-aligned at y = 0.
#
# TASK 6 COSTS ONE CROSSING ON isp-core (4 -> 5). Recorded here, not hidden.
# It is not a defect in the port: `routeEdge`'s parallel-edge fan is a FIXED
# perpendicular offset that does not adapt to uneven row gaps, so once y stops
# being a rigid uniform grid the sampled curve geometry shifts and a near-miss
# can flip. The reference prototype hit the same wall and documented it. What
# the crossing buys is the entire point of the phase: every column is centred,
# so `local` radiates from the middle of the map instead of raking
# down-and-right out of the top-left corner, and a node sits level with the
# neighbours it links to. Measured alternative, for the record: keeping the
# rigid grid and ONLY centring scores 4/6 -- one crossing better, but it
# abandons the neighbour-median placement the design asks for.
#
# dp_swallowed reached 0 on both fixtures in Task 4 and has STAYED 0 through
# both later phases (identical across chromium/firefox/webkit).
#
# See this module's docstring: dp_count/management_count/tunnel_count pin the
# CLASSIFICATION itself, so a bug that silently miscounts edges (e.g. a
# `data-provenance` attribute that goes missing or stops matching) fails
# loudly here instead of the crossings/swallowed budget quietly passing over
# the wrong edge set.
BUDGETS = {
    "isp-core.json": {
        # PINS ADJUSTED (topology-default-view spec, isp-core fixture
        # touch-up): sgw-01 was fused into the "mme-01" chassis element and
        # hss-01 into "pgw-01" (docs-hero shot -- two more physical chassis,
        # no new hosts). dp_count is UNCHANGED at 30: declared links are
        # never collapsed by element pair (kept individual, even parallel),
        # so the raw link count doesn't move. management_count drops 23 -> 21:
        # each fused pair used to contribute TWO `local:<element>` edges (one
        # per singleton element) and now contributes only one (one physical
        # element, still hop-less) -- a -2, not a -1, because BOTH chassis
        # lost a duplicate. PIN ADJUSTED AGAIN (addendum review, findings
        # 2+3): tunnel_count goes 2 -> 5. The addendum caught the degraded
        # tunnel NOT actually riding (its 2-hop pe-02/agg-03 hop pair had no
        # underlying link, so it rendered as a bare/dynamic fallback chord
        # despite the comment above claiming otherwise) and the frame
        # falling short of "all three tri-state badges" (only degraded +
        # uncertain were present, no ok tunnel). Fixed by re-routing the
        # degraded tunnel over its real 2-link path (pe-02-core-02-agg-03,
        # riding pe02-core02 + agg03-core02: 2 segments) and adding a new ok
        # tunnel over another real 2-link path (pe-01-core-01-agg-01,
        # riding pe01-core01 + agg01-core01: 2 segments), alongside the
        # unchanged 1-segment uncertain tunnel: 1 + 2 + 2 = 5. dp_count/
        # management_count/dp_crossings/dp_swallowed are unaffected --
        # riding tunnel segments reuse an existing declared link's slot
        # rather than adding a new one, and no host/link/element changed --
        # measured directly against the live DOM (probe run, reverted).
        "dp_count": 30,
        "management_count": 21,
        "tunnel_count": 5,
        "dp_crossings": 5,  # 75 -> 15 (Task 4) -> 4 (Task 5) -> 5 (Task 6, see above)
        "dp_swallowed": 0,  # 0 throughout
    },
    "sprawl.json": {
        "dp_count": 16,
        "management_count": 19,
        # 1 -> 3 (Task 12): sprawl.json carries TWO tunnels
        # (tun-000000a9db01-15002, 3 hops -> 2 segments; tun-0000jumpzeph-15004,
        # 2 hops -> 1 segment) since the fixtures grew a second one (commit
        # 2209e6b) -- this budget pinned the count from before that landed and
        # was never reconciled, so `_wait_for_links`'s EXACT total (below)
        # could never be reached (measured directly against the live DOM:
        # dpCount 16, managementCount 19, tunnelCount 3 -- the other two
        # numbers already matched).
        "tunnel_count": 3,
        "dp_crossings": 6,  # 21 -> 13 (Task 4) -> 6 (Task 5) -> 6 (Task 6, unchanged)
        "dp_swallowed": 0,  # was 3 before Task 4
    },
}

# Exact total rendered-edge count per fixture, once settled -- see
# `_wait_for_links`'s docstring in test_review_shell.py for the race this
# guards (issue #130: `locator.count()` doesn't retry, so sampling the DOM
# in the ~1-frame window before React Flow has measured both endpoints of
# every edge reads a partial, sometimes-empty set). Derived from BUDGETS
# (dp + management + tunnel) rather than hand-kept, so it can't drift from
# the counts this module already pins below. Waiting for the EXACT total
# instead of a loose floor is strictly tighter: if React Flow ever commits
# edges in more than one batch, a partial-but-above-floor read would sample
# mid-batch and fail the exact dp_count/management_count/tunnel_count pins
# below as a flake instead of this wait catching it first.
TOTAL_EDGES = {
    fixture: expected["dp_count"] + expected["management_count"] + expected["tunnel_count"]
    for fixture, expected in BUDGETS.items()
}


def _import_fixture(page, name: str) -> None:
    page.locator('[data-testid="import-input"]').set_input_files(FIXTURES / name)
    page.locator('[data-testid="review-bar"]').wait_for()


def _wait_for_links(page, exactly: int) -> None:
    page.wait_for_function(
        "(n) => document.querySelectorAll('[data-testid^=\"topo-link-\"]').length === n",
        arg=exactly,
    )


# The five real values of TopoEdge["provenance"] (web/src/data/topology.ts).
_VALID_PROVENANCE = {"declared", "implicit", "dynamic", "local", "reports-for"}


def test_edges_expose_provenance_in_the_dom(shell_dash, page):
    """LinkEdge.tsx stamps `data-provenance={edge.provenance}` on its
    `<g data-testid="topo-link-...">` wrapper, and the budget test above reads
    that attribute directly to classify edges rather than guessing from the
    edge id or the rendered stroke (see this module's docstring). This is a
    unit-level check that belongs next to LinkEdge.tsx in vitest, but
    rendering it there needs a real React Flow context: xyflow only computes
    an edge's source/target handle positions from actual DOM layout
    (getBoundingClientRect on the handle elements), which jsdom never
    performs -- a `<ReactFlow>` mounted in vitest renders the node wrappers
    but the edge's `<g>` never appears (verified: `.react-flow__edges` stays
    empty even with explicit node `width`/`height` and a `ResizeObserver`
    shim). So this asserts it here instead, against the real rendered DOM,
    directly and independently of the crossings/swallowed numbers above: if
    `data-provenance` were ever dropped, every edge would fall through
    `classify()`'s default branch to "management" and the dp_count/
    tunnel_count pins would drift too -- but only as an indirect symptom.
    """
    page.goto(shell_dash.url)
    _import_fixture(page, "isp-core.json")
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topology-page"]').wait_for()
    _wait_for_links(page, TOTAL_EDGES["isp-core.json"])

    provenances = page.eval_on_selector_all(
        '[data-testid^="topo-link-"]',
        "els => els.map((el) => el.getAttribute('data-provenance'))",
    )

    assert provenances, "expected at least one rendered link"
    assert all(p in _VALID_PROVENANCE for p in provenances), (
        f"every rendered link must carry a real data-provenance value, got {provenances}"
    )
    assert "declared" in provenances, "isp-core.json has declared (data-plane) links"


# Re-implements measure.ts's three rules directly against the rendered DOM:
#
# - Node rects come from React Flow's OWN `.react-flow__node` wrapper (its
#   library-generated `data-testid="rf__node-<id>"`, not our app's), reading
#   the wrapper's `translate(x,y)` inline style plus `offsetWidth`/
#   `offsetHeight` -- the same MODEL-space rect `LinkEdge.tsx`'s `rectOf()`
#   feeds into `routeEdge`, not a screen-space `getBoundingClientRect()`
#   (which would be wrong once panned/zoomed away from the initial fit).
# - Edge classification reads `edge.provenance` itself, off the
#   `data-provenance` attribute LinkEdge.tsx sets on its `<g data-testid>`
#   wrapper -- the same element and the same seam Playwright already used for
#   `topo-link-<id>`. This mirrors measure.ts's own `classifyEdge`: `declared`
#   -> data-plane, `dynamic` -> tunnel, everything else (`implicit`/`local`/
#   `reports-for`) -> management. Earlier drafts of this test guessed
#   provenance instead -- management from an id-prefix regex, tunnel by
#   sniffing the stroke-dasharray out of the CSSOM -- and both were wrong in
#   the same way: a declared link's `name` (lab.json) is a free-form,
#   unvalidated string that `topology.ts` uses verbatim as the edge id, so a
#   link literally named e.g. `local:backbone` would have been misclassified
#   as management and silently dropped from the budget; and the dasharray
#   sniff coupled the metric to edgeStyles.ts's styling, so changing a
#   tunnel's dash would have silently reclassified it as data-plane. Reading
#   the attribute makes the classifier read PROVENANCE, not infer it.
# - Source/target per edge come from React Flow's own default
#   `aria-label="Edge from <source> to <target>"` on the `rf__edge-<id>`
#   wrapper (set whenever the app doesn't override `ariaLabel`, which
#   TopologyPage.tsx doesn't) -- needed so `countSwallowed` can exclude an
#   edge's own endpoints and `countCrossings` can exclude edges that merely
#   meet at a shared node.
# - The visible path is `path.react-flow__edge-path` specifically (BaseEdge's
#   own class), NOT a `:not(.react-flow__edge-interaction)` negation: a
#   tunnel edge also draws a casing `<path>` with no class of its own, which
#   a bare negation would pick up as a second "visible" path per edge.
_MEASURE_JS = """
() => {
  const SAMPLES = __SAMPLES__;

  const nodeRects = new Map();
  for (const el of document.querySelectorAll('.react-flow__node')) {
    const testid = el.getAttribute('data-testid') || '';
    const id = testid.replace(/^rf__node-/, '');
    const m = (el.style.transform || '').match(/translate\\(([-\\d.]+)px,\\s*([-\\d.]+)px\\)/);
    nodeRects.set(id, {
      x: m ? parseFloat(m[1]) : 0,
      y: m ? parseFloat(m[2]) : 0,
      width: el.offsetWidth,
      height: el.offsetHeight,
    });
  }

  // Mirrors measure.ts's classifyEdge: declared -> data-plane, dynamic ->
  // tunnel, everything else (implicit/local/reports-for) -> management.
  function classify(provenance) {
    if (provenance === 'declared') return 'data-plane';
    if (provenance === 'dynamic') return 'tunnel';
    return 'management';
  }

  const edges = [];
  for (const wrapper of document.querySelectorAll('.react-flow__edge')) {
    const id = (wrapper.getAttribute('data-testid') || '').replace(/^rf__edge-/, '');
    const label = wrapper.getAttribute('aria-label') || '';
    const match = label.match(/^Edge from (.+) to (.+)$/);
    if (!match) continue;
    const [, source, target] = match;
    const visible = wrapper.querySelector('path.react-flow__edge-path');
    if (!visible) continue;
    // LinkEdge.tsx stamps the real provenance on its <g data-testid> wrapper
    // (the same element `topo-link-<id>` lives on) -- read it directly
    // rather than inferring it from the id or the rendered stroke.
    const linkG = wrapper.querySelector(`[data-testid="topo-link-${id}"]`);
    const provenance = linkG ? linkG.getAttribute('data-provenance') : null;
    const category = classify(provenance);
    edges.push({ id, source, target, category, path: visible.getAttribute('d') });
  }

  // Mirrors measure.ts's samplePath: the exact M/L/C grammar routeEdge
  // emits, throwing (not silently skipping) on anything else.
  function samplePath(d, n) {
    const commands = d.match(/[A-Za-z]/g) || [];
    for (const c of commands) {
      if (c !== 'M' && c !== 'L' && c !== 'C') {
        throw new Error('unsupported path command "' + c + '" in "' + d + '"');
      }
    }
    const nums = (d.match(/-?\\d*\\.?\\d+(?:e[-+]?\\d+)?/gi) || []).map(Number);
    const pts = [];
    if (commands.length === 2 && commands[0] === 'M' && commands[1] === 'L' && nums.length === 4) {
      const [sx, sy, tx, ty] = nums;
      for (let i = 0; i < n; i++) {
        const t = n <= 1 ? 0 : i / (n - 1);
        pts.push({ x: sx + (tx - sx) * t, y: sy + (ty - sy) * t });
      }
      return pts;
    }
    if (commands.length === 2 && commands[0] === 'M' && commands[1] === 'C' && nums.length === 8) {
      const [sx, sy, c1x, c1y, c2x, c2y, tx, ty] = nums;
      for (let i = 0; i < n; i++) {
        const t = n <= 1 ? 0 : i / (n - 1);
        const u = 1 - t;
        pts.push({
          x: u * u * u * sx + 3 * u * u * t * c1x + 3 * u * t * t * c2x + t * t * t * tx,
          y: u * u * u * sy + 3 * u * u * t * c1y + 3 * u * t * t * c2y + t * t * t * ty,
        });
      }
      return pts;
    }
    throw new Error('unsupported path grammar "' + d + '"');
  }

  const dataPlane = edges.filter((e) => e.category === 'data-plane');

  const inRect = (p, r) =>
    p.x >= r.x && p.x <= r.x + r.width && p.y >= r.y && p.y <= r.y + r.height;
  const swallowedIds = [];
  for (const e of dataPlane) {
    const pts = samplePath(e.path, SAMPLES);
    let hit = false;
    for (const [nid, rect] of nodeRects) {
      if (nid === e.source || nid === e.target) continue;
      if (pts.some((p) => inRect(p, rect))) { hit = true; break; }
    }
    if (hit) swallowedIds.push(e.id);
  }

  function segInt(p1, p2, p3, p4) {
    const d1x = p2.x - p1.x, d1y = p2.y - p1.y;
    const d2x = p4.x - p3.x, d2y = p4.y - p3.y;
    const denom = d1x * d2y - d1y * d2x;
    if (Math.abs(denom) < 1e-9) return false;
    const t = ((p3.x - p1.x) * d2y - (p3.y - p1.y) * d2x) / denom;
    const u = ((p3.x - p1.x) * d1y - (p3.y - p1.y) * d1x) / denom;
    return t > 0.001 && t < 0.999 && u > 0.001 && u < 0.999;
  }

  const dpPolylines = dataPlane.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    pts: samplePath(e.path, SAMPLES),
  }));
  const crossingPairs = [];
  for (let i = 0; i < dpPolylines.length; i++) {
    for (let j = i + 1; j < dpPolylines.length; j++) {
      const a = dpPolylines[i], b = dpPolylines[j];
      if (
        a.source === b.source || a.source === b.target ||
        a.target === b.source || a.target === b.target
      ) continue;
      let found = false;
      for (let k = 0; k < a.pts.length - 1 && !found; k++) {
        for (let l = 0; l < b.pts.length - 1 && !found; l++) {
          if (segInt(a.pts[k], a.pts[k + 1], b.pts[l], b.pts[l + 1])) found = true;
        }
      }
      if (found) crossingPairs.push([a.id, b.id]);
    }
  }

  return {
    totalEdges: edges.length,
    dpCount: dataPlane.length,
    managementCount: edges.filter((e) => e.category === 'management').length,
    tunnelCount: edges.filter((e) => e.category === 'tunnel').length,
    dpCrossings: crossingPairs.length,
    dpSwallowed: swallowedIds.length,
    swallowedIds,
    crossingPairs,
  };
}
""".replace("__SAMPLES__", str(SAMPLES))


def _measure(page) -> dict:
    return page.evaluate(_MEASURE_JS)


@pytest.mark.parametrize(("fixture", "expected"), sorted(BUDGETS.items()))
def test_layout_budget(shell_dash, page, fixture, expected):
    """The default (inter-element, sources-off) topology view stays within
    today's measured crossing/swallowed budget for a real, lab-rich fixture."""
    page.goto(shell_dash.url)
    _import_fixture(page, fixture)
    page.goto(f"{shell_dash.url}#/topology")
    page.locator('[data-testid="topology-page"]').wait_for()
    _wait_for_links(page, TOTAL_EDGES[fixture])

    metrics = _measure(page)

    # Pin the classification itself first: if this drifts, the
    # crossings/swallowed numbers below are being computed over the wrong
    # edge set and would otherwise fail (or pass) for the wrong reason.
    assert metrics["dpCount"] == expected["dp_count"], (
        f"{fixture}: expected {expected['dp_count']} data-plane edges, "
        f"measured {metrics['dpCount']} (total {metrics['totalEdges']})"
    )
    assert metrics["managementCount"] == expected["management_count"], (
        f"{fixture}: expected {expected['management_count']} management edges, "
        f"measured {metrics['managementCount']}"
    )
    assert metrics["tunnelCount"] == expected["tunnel_count"], (
        f"{fixture}: expected {expected['tunnel_count']} tunnel edges, "
        f"measured {metrics['tunnelCount']}"
    )

    assert metrics["dpSwallowed"] <= expected["dp_swallowed"], (
        f"{fixture}: dp_swallowed budget exceeded: {metrics['dpSwallowed']} > "
        f"{expected['dp_swallowed']} (swallowed edges: {metrics['swallowedIds']})"
    )
    assert metrics["dpCrossings"] <= expected["dp_crossings"] + CROSSING_MARGIN, (
        f"{fixture}: dp_crossings budget exceeded: {metrics['dpCrossings']} > "
        f"{expected['dp_crossings']} + {CROSSING_MARGIN} margin "
        f"(crossing pairs: {metrics['crossingPairs']})"
    )
