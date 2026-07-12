"""Lab snapshot builder — static links only, never credentials (spec 2026-07-10 §2/§3)."""

from otto.host.login_proxy import Cred
from otto.host.unix_host import UnixHost
from otto.link.model import Link, LinkEndpoint, Provenance
from otto.monitor.session import snapshot_lab


def _host(**over):
    base = {
        "ip": "10.0.0.1",
        "element": "gw",
        "creds": [Cred(login="admin", password="hunter2")],
    }
    base.update(over)
    return UnixHost(**base)


def test_snapshot_hosts_and_implicit_links():
    gw = _host()
    n1 = _host(element="rack", board="n1", slot=2, hop="gw", ip="10.0.0.2")

    snap = snapshot_lab([gw, n1], declared=[])

    assert {h.id for h in snap.hosts} == {"gw", "rack_n12"}
    n1_snap = next(h for h in snap.hosts if h.hop == "gw")
    assert n1_snap.slot == 2
    assert n1_snap.board == "n1"
    assert n1_snap.element == "rack"
    assert snap.elements == []
    # EXACTLY one link: n1's hop="gw" edge. gw is hop-less, so implicit_links
    # also wires it to the "local" pseudo-root — that edge is filtered out
    # because "local" is never a snapshot host (see snapshot_lab).
    assert len(snap.links) == 1
    (link,) = snap.links
    assert link.provenance == "implicit"
    assert {e.host for e in link.endpoints} == {"gw", "rack_n12"}


def test_snapshot_drops_links_with_unresolvable_endpoints():
    # gw is hop-less -> implicit_links emits a gw<->local edge; "local" is not a
    # snapshot host (LocalHost is not a RemoteHost), so it must not be exported.
    gw = _host()
    # orphan's hop names a host outside this snapshot -> its implicit edge dangles.
    orphan = _host(element="orphan", ip="10.0.0.9", hop="not_in_snapshot")
    # A declared link pointing at an unknown host is equally dead.
    dangling_declared = Link(
        a=LinkEndpoint(host="gw", ip=gw.ip),
        b=LinkEndpoint(host="nowhere", ip="10.9.9.9"),
        provenance=Provenance.DECLARED,
    )

    snap = snapshot_lab([gw, orphan], declared=[dangling_declared])

    assert {h.id for h in snap.hosts} == {"gw", "orphan"}
    assert snap.links == []
    assert "local" not in snap.model_dump_json()


def test_snapshot_never_carries_credentials():
    gw = _host()

    snap = snapshot_lab([gw], declared=[])
    dumped = snap.model_dump_json()

    assert "password" not in dumped
    assert "login" not in dumped
    assert "hunter2" not in dumped
    assert "admin" not in dumped


def test_declared_link_impair_passthrough():
    gw = _host()
    n1 = _host(element="rack", board="n1", slot=2, hop="gw", ip="10.0.0.2")
    declared_link = Link(
        a=LinkEndpoint(host="gw", ip=gw.ip),
        b=LinkEndpoint(host="rack_n12", ip=n1.ip),
        protocol="udp",
        provenance=Provenance.DECLARED,
        impair="mb-1",
    )

    snap = snapshot_lab([gw, n1], declared=[declared_link])
    declared = [link for link in snap.links if link.provenance == "declared"]

    assert len(declared) == 1
    assert declared[0].impair == "mb-1"
    assert declared[0].protocol == "udp"
