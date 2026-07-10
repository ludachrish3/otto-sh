"""Static link derivation: declared resolution + implicit hop edges."""

import pytest

from otto.link import Provenance
from otto.link.derive import HostAddressing, implicit_links, resolve_declared_links

CARROT = HostAddressing(ip="10.10.200.11", interfaces={"eth1": "192.168.1.11"})
TOMATO = HostAddressing(
    ip="10.10.200.12", interfaces={"eth1": "192.168.1.12", "eth2": "192.168.2.12"}
)
BARE = HostAddressing(ip="10.10.200.13", interfaces={})

HOSTS = {"carrot_seed": CARROT, "tomato_seed": TOMATO, "basil_seed": BARE}


def _entry(**overrides) -> dict:
    base = {
        "endpoints": [
            {"host": "carrot_seed", "interface": "eth1"},
            {"host": "tomato_seed", "interface": "eth1"},
        ],
        "protocol": "udp",
    }
    return {**base, **overrides}


class TestResolveDeclaredLinks:
    def test_resolves_named_interfaces(self):
        (link,) = resolve_declared_links(
            [_entry()], HOSTS, source="lab.json", loaded_ids=set(HOSTS)
        )
        assert link.provenance is Provenance.DECLARED
        ips = {link.a.ip, link.b.ip}
        assert ips == {"192.168.1.11", "192.168.1.12"}

    def test_omitted_interface_single_iface_host_assumed(self):
        entry = _entry(endpoints=[{"host": "carrot_seed"}, {"host": "basil_seed"}])
        (link,) = resolve_declared_links([entry], HOSTS, source="lab.json", loaded_ids=set(HOSTS))
        by_host = {e.host: e for e in (link.a, link.b)}
        assert by_host["carrot_seed"].interface == "eth1"  # sole iface assumed
        assert by_host["carrot_seed"].ip == "192.168.1.11"
        assert by_host["basil_seed"].interface is None  # no ifaces -> mgmt ip
        assert by_host["basil_seed"].ip == "10.10.200.13"

    def test_omitted_interface_multi_iface_host_errors(self):
        entry = _entry(endpoints=[{"host": "tomato_seed"}, {"host": "basil_seed"}])
        with pytest.raises(ValueError, match=r"ambiguous interface.*eth1.*eth2"):
            resolve_declared_links([entry], HOSTS, source="lab.json", loaded_ids=set(HOSTS))

    def test_unknown_host_errors(self):
        entry = _entry(endpoints=[{"host": "nope"}, {"host": "basil_seed"}])
        with pytest.raises(ValueError, match="unknown host 'nope'"):
            resolve_declared_links([entry], HOSTS, source="lab.json", loaded_ids=set(HOSTS))

    def test_unknown_interface_errors(self):
        entry = _entry(
            endpoints=[{"host": "carrot_seed", "interface": "eth9"}, {"host": "basil_seed"}]
        )
        with pytest.raises(ValueError, match="no interface 'eth9'"):
            resolve_declared_links([entry], HOSTS, source="lab.json", loaded_ids=set(HOSTS))

    def test_error_names_source_and_index(self):
        entry = _entry(endpoints=[{"host": "nope"}, {"host": "basil_seed"}])
        with pytest.raises(ValueError, match=r"lab\.json.*index 0"):
            resolve_declared_links([entry], HOSTS, source="lab.json", loaded_ids=set(HOSTS))

    def test_unrelated_link_skipped_not_resolved(self):
        """A link whose BOTH endpoints lie outside ``loaded_ids`` is skipped, so
        even an otherwise-broken entry (unknown host, malformed shape) cannot
        raise — containment symmetric with cross-lab host records.
        """
        bad_unknown = _entry(endpoints=[{"host": "ghost"}, {"host": "phantom"}])
        malformed = {"endpoints": [{"host": "ghost"}]}  # 1 endpoint: would fail LinkSpec
        assert (
            resolve_declared_links(
                [bad_unknown, malformed], HOSTS, source="lab.json", loaded_ids={"carrot_seed"}
            )
            == []
        )

    def test_touching_link_resolved_even_with_dangling_endpoint(self):
        """>= 1 endpoint in ``loaded_ids`` -> resolved; the other end still
        resolves from ``hosts`` even though it is outside the lab."""
        entry = _entry(endpoints=[{"host": "carrot_seed"}, {"host": "basil_seed"}])
        (link,) = resolve_declared_links(
            [entry], HOSTS, source="lab.json", loaded_ids={"carrot_seed"}
        )
        assert {link.a.host, link.b.host} == {"carrot_seed", "basil_seed"}


class TestImpairField:
    def test_impair_carried_onto_link(self):
        hosts = {**HOSTS, "wanem_seed": HostAddressing(ip="10.10.200.14", interfaces={})}
        entry = _entry(impair="wanem_seed")
        (link,) = resolve_declared_links([entry], hosts, source="lab.json", loaded_ids=set(hosts))
        assert link.impair == "wanem_seed"

    def test_unknown_impair_host_rejected(self):
        entry = _entry(impair="wanem_seed")
        with pytest.raises(ValueError, match="impair host 'wanem_seed' is not a known host"):
            resolve_declared_links([entry], HOSTS, source="lab.json", loaded_ids=set(HOSTS))

    def test_impair_host_must_not_be_an_endpoint(self):
        entry = _entry(impair="carrot_seed")
        with pytest.raises(ValueError, match="is an endpoint of the link"):
            resolve_declared_links([entry], HOSTS, source="lab.json", loaded_ids=set(HOSTS))


class _FakeHost:
    def __init__(
        self, host_id: str, ip: str = "203.0.113.1", hop: str | None = None, term: str = "ssh"
    ):
        self.id, self.ip, self.hop, self.term = host_id, ip, hop, term


class TestImplicitLinks:
    def test_hop_edge_per_hopped_host(self):
        hosts = {
            "local": _FakeHost("local", ip="127.0.0.1"),
            "gw": _FakeHost("gw"),
            "sprout1": _FakeHost("sprout1", hop="gw", term="telnet"),
        }
        links = implicit_links(hosts)
        by_pair = {frozenset((link.a.host, link.b.host)): link for link in links}
        assert frozenset(("gw", "sprout1")) in by_pair
        assert by_pair[frozenset(("gw", "sprout1"))].protocol == "telnet"
        assert all(link.provenance is Provenance.IMPLICIT for link in links)

    def test_hopless_host_attaches_to_local_root(self):
        hosts = {"local": _FakeHost("local", ip="127.0.0.1"), "gw": _FakeHost("gw")}
        links = implicit_links(hosts)
        assert {frozenset((link.a.host, link.b.host)) for link in links} == {
            frozenset(("local", "gw"))
        }

    def test_local_itself_emits_no_edge(self):
        assert implicit_links({"local": _FakeHost("local")}) == []

    def test_missing_hop_target_still_edges_with_empty_ip(self):
        hosts = {"sprout1": _FakeHost("sprout1", hop="ghost")}
        (edge,) = [link for link in implicit_links(hosts) if "ghost" in (link.a.host, link.b.host)]
        ghost = edge.a if edge.a.host == "ghost" else edge.b
        assert ghost.ip == ""
