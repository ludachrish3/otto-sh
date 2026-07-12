"""Invariants of the committed monitor fixtures (spec 2026-07-10 §5).

The generator builds documents THROUGH the export models, so schema
conformance is by construction; these tests pin the *content* contracts the
UI phase relies on: determinism, subject resolvability, the outage window,
drift across sessions, and the fixture-only status of the dynamic link.
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
    assert set(docs) == {"kitchen-sink", "minimal", "drift", "cascade"}
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
    # exactly one dynamic link, fixture-only (spec §2)
    assert sum(1 for ln in s.lab.links if ln.provenance == "dynamic") == 1
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


def test_no_dynamic_links_outside_kitchen_sink():
    docs = build_all()
    for stem in ("minimal", "drift"):
        for s in docs[stem].sessions:
            assert not [ln for ln in s.lab.links if ln.provenance == "dynamic"]


def test_no_credentials_anywhere():
    for doc in build_all().values():
        text = dumps(doc)
        for needle in ("password", "creds", "login"):
            assert needle not in text, needle


def test_size_caps():
    for stem, doc in build_all().items():
        assert len(dumps(doc)) < 3_500_000, stem
