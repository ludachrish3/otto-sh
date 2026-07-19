"""Tunnels in the topology view (spec 2026-07-16, Task 12): riding geometry,
the bare-tunnel fallback, whole-tunnel selection (incl. the cross-segment
emphasis carried over from the Task 10 review — previously exercised by
NOTHING), the uncertain-tunnel ghost, and live churn that must never move a
node.

Contract: ``data-testid``/``data-tunnel``/``data-tunnel-status`` attributes
(Tasks 10-11) and the ``inspector-tunnel-*`` testids — styling and DOM
structure otherwise free to change. Review-mode specs drive the built shell
through the client-side Import front door (``shell_dash`` + a local
``_import_fixture``, exactly as ``test_review_shell.py`` and
``test_topology_budget.py`` already do for ``sprawl.json``/``isp-core.json``
— there is no server-side way to select a specific fixture document; the
brief's ``review_dash`` was scaffolding, and the real fixture in this
directory's ``conftest.py`` is hardcoded to ``minimal.json`` for the boot-
hydration specs only). The live spec drives ``live_stream_dash`` + a fake
SSE tunnel publish, mirroring ``test_live_shell.py``.
"""

from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Locator, Page

from tests._fixtures._dashboard_harness import DashboardHarness
from tests._fixtures._fake_collector import FakeCollector

pytestmark = [
    pytest.mark.hostless,
    pytest.mark.browser,
]

FIXTURES = Path(__file__).resolve().parents[4] / "web" / "fixtures"


def _import_fixture(page: Page, name: str) -> None:
    page.locator('[data-testid="import-input"]').set_input_files(FIXTURES / name)
    page.locator('[data-testid="review-bar"]').wait_for()


def _tid(page: Page, test_id: str) -> Locator:
    return page.locator(f'[data-testid="{test_id}"]')


def _path_d(page: Page, edge_id: str) -> str:
    """The BaseEdge path's `d` -- the drawn geometry of one edge or tunnel
    segment. Waits for the edge itself first: React Flow only renders an
    edge once both endpoint nodes are measured (see
    ``test_review_shell.py``'s ``_wait_for_links`` docstring), so sampling
    this the instant the canvas mounts races that measurement."""
    sel = f'[data-testid="topo-link-{edge_id}"] path.react-flow__edge-path'
    page.locator(sel).wait_for()
    return page.eval_on_selector(sel, "el => el.getAttribute('d')")


def _point_on_edge(page: Page, edge_id: str) -> dict:
    """A point on an edge's actual rendered stroke, not its naive bounding-box
    center -- copied from ``test_review_shell.py``'s helper of the same name
    (see its docstring for the full rationale: a curved/bowed edge can leave
    its own bbox center over an unrelated node or bare pane, and even a
    ``force=True`` click still hit-tests for real once dispatched). Tunnel
    segments render through the same ``LinkEdge``/``BaseEdge`` machinery as
    every other edge, including the wide invisible
    ``react-flow__edge-interaction`` pointer target this samples along, so
    the same technique applies unchanged -- and is used here from the start
    (not just as a WebKit fallback) since a plain bbox click was already
    proven unreliable across engines for exactly this class of edge."""
    path = page.locator(f'[data-testid="topo-link-{edge_id}"] path.react-flow__edge-interaction')
    point = path.evaluate(
        """(el) => {
            const total = el.getTotalLength();
            for (let i = 1; i < 20; i++) {
                const pt = el.getPointAtLength(total * (i / 20));
                const ctm = el.getScreenCTM();
                const x = ctm.a * pt.x + ctm.c * pt.y + ctm.e;
                const y = ctm.b * pt.x + ctm.d * pt.y + ctm.f;
                const top = document.elementFromPoint(x, y);
                if (top === el || el.parentElement.contains(top)) return { x, y };
            }
            return null;
        }"""
    )
    assert point is not None, f"no pointer-target point found along edge {edge_id}'s stroke"
    return point


def _click_edge(page: Page, edge_id: str) -> None:
    """Click a topology edge's (or tunnel segment's) actual rendered stroke."""
    point = _point_on_edge(page, edge_id)
    page.mouse.click(point["x"], point["y"])


# ── Review mode (fixtures, client-side Import) ─────────────────────────────


def test_bare_tunnel_renders_as_a_dynamic_edge(page: Page, shell_dash: Any) -> None:
    """kitchen-sink's tun-00000000demo-15001: no underlay (edge-gw -> db-01
    has no declared link between them) -> one bare dynamic segment."""
    page.goto(shell_dash.url)
    _import_fixture(page, "kitchen-sink.json")
    page.goto(f"{shell_dash.url}#/topology")
    seg = page.locator('[data-tunnel="tun-00000000demo-15001"]')
    seg.first.wait_for()
    assert seg.count() == 1
    assert seg.first.get_attribute("data-tunnel-status") == "ok"


def test_riding_segments_reproduce_their_underlay_geometry(page: Page, shell_dash: Any) -> None:
    """sprawl's tun-000000a9db01-15002 (tor-sw-a -> app-01 -> db-01, status
    degraded): both segments ride a declared underlay -- the drawn path `d`
    of each segment equals its underlay's exactly.

    The pairing is deterministic, not just "one of the two": segment `:0`
    walks hop pair (tor-sw-a, app-01), whose only declared link is
    `app01-tor`; segment `:1` walks (app-01, db-01), whose only declared
    link is `app01-db` (see sprawl.json's ``lab.links``). ``routeEdge``
    (web/src/topo/routing.ts) is explicitly symmetric in source/target --
    it orders by rect position (upper/lower or left/right), never by which
    argument was called "source" -- so this holds even though a segment's
    own (source, target) can be the underlay's reversed (segment `:0` runs
    tor-sw-a -> app-01; the `app01-tor` link's endpoints put its source at
    app-01, target at tor-sw-a).
    """
    page.goto(shell_dash.url)
    _import_fixture(page, "sprawl.json")
    page.goto(f"{shell_dash.url}#/topology")

    seg0 = _path_d(page, "tun-000000a9db01-15002:0")
    seg1 = _path_d(page, "tun-000000a9db01-15002:1")
    tor_underlay = _path_d(page, "app01-tor")
    db_underlay = _path_d(page, "app01-db")

    assert seg0 == tor_underlay
    assert seg1 == db_underlay
    assert seg0 != seg1


def test_clicking_any_segment_selects_the_whole_tunnel(page: Page, shell_dash: Any) -> None:
    """Clicking one segment of the sprawl 3-hop tunnel opens the tunnel
    inspector for the WHOLE tunnel, and emphasizes EVERY segment -- not just
    the one clicked (Task 10 review carry-over: this cross-segment emphasis
    was, until this test, exercised by nothing). ``TopologyPage``'s
    `sameTunnel` check applies `tunnelEmphasized` to every edge sharing the
    selected edge's `tunnel.id`, independent of React Flow's own per-edge
    `selected` flag (this page passes a fully externally-controlled `edges`
    array and never sets that flag itself)."""
    page.goto(shell_dash.url)
    _import_fixture(page, "sprawl.json")
    page.goto(f"{shell_dash.url}#/topology")

    _click_edge(page, "tun-000000a9db01-15002:0")

    panel = _tid(page, "inspector-tunnel")
    panel.wait_for()
    assert _tid(page, "inspector-tunnel-status").inner_text().endswith("degraded")
    assert "4/6" in _tid(page, "inspector-tunnel-carriers").inner_text()
    assert "tor-sw-a → app-01 → db-01" in _tid(page, "inspector-tunnel-path").inner_text()

    # Every segment of the tunnel is emphasized, not just the clicked one:
    # base tunnel stroke width is 2 (EDGE_STYLES.tunnel, edgeStyles.ts) --
    # emphasis adds EMPHASIS_WIDTH (1.5) on top, so a plain > 2 is
    # discriminating without hardcoding the exact sum.
    segment_ids = [f"tun-000000a9db01-15002:{i}" for i in range(2)]
    page.wait_for_function(
        """(ids) => ids.every((id) => {
            const p = document.querySelector(
                `[data-testid="topo-link-${id}"] path.react-flow__edge-path`
            );
            return p !== null && parseFloat(getComputedStyle(p).strokeWidth) > 2;
        })""",
        arg=segment_ids,
    )
    for seg_id in segment_ids:
        width = page.locator(
            f'[data-testid="topo-link-{seg_id}"] path.react-flow__edge-path'
        ).evaluate("el => parseFloat(getComputedStyle(el).strokeWidth)")
        assert width > 2  # base tunnel width 2 + EMPHASIS_WIDTH


def test_uncertain_tunnel_ghosts(page: Page, shell_dash: Any) -> None:
    """isp-core's tun-0000jumpacc7-15003 (jump-01 -> acc-07, status
    uncertain): ghosted -- ``tunnelEdgeStyle`` fades an uncertain,
    unemphasized segment to opacity 0.4 (UNCERTAIN_OPACITY)."""
    page.goto(shell_dash.url)
    _import_fixture(page, "isp-core.json")
    page.goto(f"{shell_dash.url}#/topology")

    seg = page.locator('[data-tunnel="tun-0000jumpacc7-15003"]').first
    seg.wait_for()
    assert seg.get_attribute("data-tunnel-status") == "uncertain"
    opacity = seg.locator("path.react-flow__edge-path").evaluate(
        "el => parseFloat(getComputedStyle(el).opacity)"
    )
    assert opacity < 1


# ── Live mode (SSE) ─────────────────────────────────────────────────────────

# h1/h2 are members of live_stream_dash's declared lab (_LIVE_STREAM_HOSTS,
# conftest.py) -- a tunnel hop host that isn't a member of `lab.hosts` is
# silently skipped by buildTunnelEdges (topology.ts), same rule that governs
# every other live-shell spec's host choice in this directory.
REC = {
    "id": "tun-live0000001-15009",
    "protocol": "udp",
    "service_port": 15009,
    "hops": ["h1", "h2"],
    "status": "ok",
    "carriers_present": 4,
    "carriers_expected": 4,
    "age_seconds": 5.0,
}


def _node_positions(page: Page) -> dict[str, str]:
    return page.eval_on_selector_all(
        ".react-flow__node",
        "els => Object.fromEntries(els.map(e => [e.dataset.id, e.style.transform]))",
    )


def test_tunnel_churn_never_moves_nodes(
    page: Page, live_stream_dash: DashboardHarness[FakeCollector]
) -> None:
    """A tunnel appearing/disappearing over SSE must redraw the overlay edge
    without ever touching node position -- ``layoutTopo`` derives columns
    from the DATA-PLANE (`declared`-link) graph only (`isDataPlaneEdge`,
    topo/layout.ts); a `dynamic` (tunnel) edge is invisible to it by
    construction, so this is a real invariant of the layout algorithm, not
    an accident of these two hosts' particular graph."""
    page.goto(f"{live_stream_dash.url}#/topology")
    page.locator('[data-testid="topo-node-h1"]').wait_for()
    page.locator('[data-testid="topo-node-h2"]').wait_for()
    before = _node_positions(page)

    live_stream_dash.run(live_stream_dash.collector.push_tunnels([REC]))
    seg = page.locator(f'[data-tunnel="{REC["id"]}"]')
    # NOT state="visible": h1/h2 land in adjacent rows of the same column
    # (neither has a declared link, so layoutTopo's backbone peel gives them
    # no column-separating structure) -- routeSameColumn's rowSpan<=1 branch
    # then draws a perfectly VERTICAL straight line (`M424,182 L424,220`,
    # verified against the live DOM). A geometrically axis-aligned SVG path's
    # `getBoundingClientRect()` is 0 in the perpendicular axis, which
    # Playwright's "visible" actionability check treats as hidden forever --
    # the same empty-bbox trap `test_review_shell.py`'s minimap wrapper hits
    # (see its own `state="attached"` comment) via a different root cause
    # (CSS `display: contents` there; a 1-D geometric bbox here).
    seg.first.wait_for(state="attached")
    assert _node_positions(page) == before

    live_stream_dash.run(live_stream_dash.collector.push_tunnels([]))
    page.wait_for_function(
        f"() => document.querySelectorAll('[data-tunnel=\"{REC['id']}\"]').length === 0"
    )
    assert _node_positions(page) == before
