"""Invariants of the committed monitor fixtures (spec 2026-07-10 §5).

The generator builds documents THROUGH the export models, so schema
conformance is by construction; these tests pin the *content* contracts the
UI phase relies on: determinism, subject resolvability, the outage window,
drift across sessions, and the fixture-only tunnels each session carries
(spec 2026-07-16 §1).
"""

import json

from otto.models import MonitorExport
from scripts.gen_monitor_fixtures import OUTAGE_S, build_all, dumps


def _subjects(doc):
    hosts = {h.id for s in doc.sessions for h in s.lab.hosts}
    elements = {e.id for s in doc.sessions for e in s.lab.elements}
    elements |= {h.element for s in doc.sessions for h in s.lab.hosts}
    return hosts, elements


def test_deterministic():
    a = {k: dumps(v) for k, v in build_all().items()}
    b = {k: dumps(v) for k, v in build_all().items()}
    assert a == b


def test_documents_round_trip_and_stems():
    docs = build_all()
    # Subset, not equality. An inventory list here has to be edited every time a
    # fixture is added — pure tax, and one of the three near-misses this
    # duplication already caused. What IS worth pinning: every stem named below
    # is read by literal filename somewhere downstream, so silently dropping one
    # from build_all() must fail HERE rather than as an ENOENT in a later gate.
    # kitchen-sink: web/src/__tests__/exportdoc.test.ts, topology.test.ts,
    #   health.test.ts, seriestree.test.ts, subjectpage.test.tsx, overview.test.tsx,
    #   pages.test.tsx, events_panel.test.tsx; and repeated
    #   _import_fixture(page, "kitchen-sink.json") calls in
    #   tests/e2e/monitor/dashboard/test_review_shell.py.
    # minimal: web/src/__tests__/reviewbar.test.tsx, reviewstore.test.ts,
    #   shell.test.tsx, bootstrap.test.ts, events_panel.test.tsx; the dashboard
    #   e2e conftest (tests/e2e/monitor/dashboard/conftest.py) reads
    #   web/fixtures/minimal.json directly to build shell_dash; and
    #   _import_fixture(page, "minimal.json") calls in test_review_shell.py.
    # drift: web/src/__tests__/reviewbar.test.tsx, reviewstore.test.ts; and
    #   _import_fixture(page, "drift.json") in test_review_shell.py.
    # cascade: web/src/__tests__/topology.test.ts; and
    #   _import_fixture(page, "cascade.json") in test_review_shell.py.
    # sprawl, isp-core: load-bearing for the topology layout-budget tests
    # (topology layout redesign, 2026-07-14) — deep management chain and
    # degenerate-hops-from-local cases kitchen-sink/cascade cannot produce.
    assert {"kitchen-sink", "minimal", "drift", "cascade", "sprawl", "isp-core"} <= set(docs)
    for doc in docs.values():
        assert MonitorExport.model_validate(json.loads(dumps(doc))) is not None


def test_every_metric_subject_resolves():
    for doc in build_all().values():
        hosts, elements = _subjects(doc)
        for s in doc.sessions:
            for m in s.metrics:
                assert m.host in hosts or m.host in elements, m.host
                if m.source is not None:
                    assert m.source in hosts, m.source


def test_kitchen_sink_shapes():
    doc = build_all()["kitchen-sink"]
    (s,) = doc.sessions
    # the empty chassis is explicit; the populated explicit element merges
    assert {e.id for e in s.lab.elements} == {"spare-chassis", "workers"}
    assert all(h.element != "spare-chassis" for h in s.lab.hosts)
    assert any(ln.impair for ln in s.lab.links)
    assert any(ln.provenance == "implicit" for ln in s.lab.links)
    # metadata holes for edge-case rendering
    assert any(h.hop is None for h in s.lab.hosts)
    assert any(h.slot is None and h.board is not None for h in s.lab.hosts)
    assert any(h.os_version is None for h in s.lab.hosts)
    # element-targeted + mgmt-sourced series exist
    assert any(m.host == "chassis-a" for m in s.metrics)
    assert any(m.source == "mgmt-01" for m in s.metrics)
    assert s.events
    assert s.log_events
    assert s.meta.charts
    assert s.chart_map


def test_kitchen_sink_outage_window():
    doc = build_all()["kitchen-sink"]
    (s,) = doc.sessions
    lo = s.start.timestamp() + OUTAGE_S[0]
    hi = s.start.timestamp() + OUTAGE_S[1]
    down = [m for m in s.metrics if m.host == "workers_w2"]
    assert down, "outage host must still have samples outside the gap"
    assert not [m for m in down if lo <= m.timestamp.timestamp() < hi]


def test_drift_sessions_evolve():
    doc = build_all()["drift"]
    assert len(doc.sessions) == 3
    labs = [s.lab.model_dump(mode="json") for s in doc.sessions]
    assert labs[0] != labs[1] != labs[2]
    s1, s2, s3 = doc.sessions
    assert len(s2.lab.hosts) > len(s1.lab.hosts)  # host added
    ids2 = {h.id for h in s2.lab.hosts}
    ids3 = {h.id for h in s3.lab.hosts}
    assert "workers_w2" in ids2  # host removed
    assert "workers_w2" not in ids3
    assert "edge-gw" not in ids2  # gateway added
    assert "edge-gw" in ids3
    slot = {
        s: next(h.slot for h in sess.lab.hosts if h.board == "lc2")
        for s, sess in (("s2", s2), ("s3", s3))
    }
    assert slot["s2"] != slot["s3"]  # board slot moved
    assert any(ln.impair for ln in s3.lab.links)  # impairment added
    assert not any(ln.impair for ln in s2.lab.links)


def test_no_credentials_anywhere():
    for doc in build_all().values():
        text = dumps(doc)
        for needle in ("password", "creds", "login"):
            assert needle not in text, needle


def test_size_caps():
    for stem, doc in build_all().items():
        assert len(dumps(doc)) < 3_500_000, stem


def test_sprawl_is_deep():
    """sprawl exists to exercise DEPTH: a 3-hop management chain, which
    kitchen-sink (max depth 1) cannot produce."""
    lab = build_all()["sprawl"].sessions[0].lab
    by_id = {h.id: h for h in lab.hosts}

    def depth(host):
        n, cur = 0, host.hop
        while cur:
            n += 1
            cur = by_id[cur].hop if cur in by_id else None
        return n

    assert max(depth(h) for h in lab.hosts) >= 3


def test_isp_core_is_shallow_but_meshed():
    """isp-core exists to exercise the DEGENERATE case the redesign targets:
    management paths are SHORT (every element 0 or 1 hops out), so the old
    hops-from-local layout collapses 23 elements into 3 columns — while the
    data plane is deep and richly meshed."""
    lab = build_all()["isp-core"].sessions[0].lab
    by_id = {h.id: h for h in lab.hosts}

    def depth(host):
        n, cur = 0, host.hop
        while cur:
            n += 1
            cur = by_id[cur].hop if cur in by_id else None
        return n

    assert max(depth(h) for h in lab.hosts) <= 1, "management paths must stay short"
    declared = [lk for lk in lab.links if (lk.provenance or "declared") == "declared"]
    assert len(declared) >= 25, "the data plane must be richly meshed"
    assert len({h.element for h in lab.hosts}) >= 20


def test_kitchen_sink_tunnels_pinned():
    """The kitchen-sink tunnel is a deliberately BARE segment: hops
    edge-gw<->db-01 with no underlay link joining them (spec 2026-07-16 §2)."""
    doc = build_all()["kitchen-sink"]
    (s,) = doc.sessions
    assert [(t.id, t.status, t.hops) for t in s.tunnels] == [
        ("tun-00000000demo-15001", "ok", ["edge-gw", "db-01"]),
    ]


def test_sprawl_tunnels_pinned():
    """sprawl carries exactly the riding 3-hop tunnel plus the 2-hop
    jump-zephyr tunnel; both content and status are pinned."""
    doc = build_all()["sprawl"]
    (s,) = doc.sessions
    got = {t.id: (t.status, t.hops) for t in s.tunnels}
    assert got == {
        "tun-000000a9db01-15002": ("degraded", ["tor-sw-a", "app-01", "db-01"]),
        "tun-0000jumpzeph-15004": ("ok", ["jump-01", "zephyr-01"]),
    }


def test_isp_core_tunnels_pinned():
    """isp-core carries the uncertain tunnel plus a degraded AND an ok tunnel
    added for the docs-hero shot (topology-default-view spec addendum): one
    frame now genuinely shows all three tri-state badges (ok/degraded/
    uncertain), each riding real declared links rather than falling back to
    a bare/dynamic chord (see test_multihop_tunnels_ride_declared_links)."""
    doc = build_all()["isp-core"]
    (s,) = doc.sessions
    got = {t.id: (t.status, t.hops) for t in s.tunnels}
    assert got == {
        "tun-0000jumpacc7-15003": ("uncertain", ["jump-01", "acc-07"]),
        "tun-0000pe02agg3-15005": ("degraded", ["pe-02", "core-02", "agg-03"]),
        "tun-0000pe01agg1-15006": ("ok", ["pe-01", "core-01", "agg-01_lc1"]),
    }


def test_multihop_tunnels_ride_declared_links():
    """The riding-overlay invariant: every consecutive hop pair of a >=3-hop
    tunnel must be joined by an actual link (declared or implicit) in the
    SAME fixture's lab -- otherwise the tunnel can't ride the data plane it
    claims to overlay (spec 2026-07-16, the riding vector).

    Deliberately general across ALL fixtures, not just sprawl: this walks
    ``tunnel.hops`` pairwise and checks link endpoint SETS, so renaming a
    link (e.g. ``app01-db``) still passes as long as a link between the pair
    exists, while deleting the link with no replacement fails. Was scoped to
    sprawl only until the addendum review caught isp-core's degraded tunnel
    silently NOT riding despite its own docstring/comment claiming it did
    (a 2-hop tunnel at the time, so even a sprawl-only >=3-hop check would
    not have caught it) -- widening this to every fixture is the permanent
    guard against that recurring.
    """
    multihop_fixtures: set[str] = set()
    for stem, doc in build_all().items():
        for s in doc.sessions:
            link_pairs = {frozenset(e.host for e in ln.endpoints) for ln in s.lab.links}
            multihop = [t for t in s.tunnels if len(t.hops) >= 3]
            if multihop:
                multihop_fixtures.add(stem)
            for tunnel in multihop:
                for a, b in zip(tunnel.hops, tunnel.hops[1:], strict=False):
                    assert frozenset((a, b)) in link_pairs, (stem, tunnel.id, a, b)
    assert {"sprawl", "isp-core"} <= multihop_fixtures, (
        "sprawl and isp-core must each carry at least one >=3-hop tunnel to exercise riding"
    )


def test_tunnel_statuses_cover_all_values():
    """Across all fixtures, every TunnelRecord status value is exercised at
    least once -- a missing one means a status the UI renders is untested."""
    statuses = {t.status for doc in build_all().values() for s in doc.sessions for t in s.tunnels}
    assert {"ok", "degraded", "uncertain"} <= statuses


def test_tunnel_hops_reference_known_hosts():
    """Every tunnel hop host id must resolve within its OWN fixture's
    lab.hosts -- a dangling hop can't be placed on the topology map."""
    for stem, doc in build_all().items():
        for s in doc.sessions:
            host_ids = {h.id for h in s.lab.hosts}
            for t in s.tunnels:
                for hop in t.hops:
                    assert hop in host_ids, (stem, t.id, hop)
