"""Static link derivation: declared resolution + implicit hop edges."""

from pathlib import Path

import pytest

from otto.labs import LabScope
from otto.link import Provenance
from otto.link.derive import (
    DeclaredLinkEntry,
    HostAddressing,
    implicit_links,
    link_attaches,
    resolve_declared_links,
)

TEST1 = HostAddressing(ip="10.10.200.11", interfaces={"eth1": "192.168.1.11"})
TEST2 = HostAddressing(
    ip="10.10.200.12", interfaces={"eth1": "192.168.1.12", "eth2": "192.168.2.12"}
)
BARE = HostAddressing(ip="10.10.200.13", interfaces={})

HOSTS = {"test1": TEST1, "test2": TEST2, "test4": BARE}


def _entry(**overrides) -> dict:
    base = {
        "endpoints": [
            {"host": "test1", "interface": "eth1"},
            {"host": "test2", "interface": "eth1"},
        ],
        "protocol": "udp",
    }
    return {**base, **overrides}


def _resolve(raw: list, hosts, *, loaded_ids) -> list:
    """Resolve *raw* entries, all declared in one ``lab.json``, for lab ``bench``."""
    entries = [
        DeclaredLinkEntry(origin=Path("lab.json"), index=i, entry=e) for i, e in enumerate(raw)
    ]
    return resolve_declared_links(
        entries, hosts, loaded_ids=loaded_ids, selection=frozenset({"bench"})
    )


class TestResolveDeclaredLinks:
    def test_resolves_named_interfaces(self):
        (link,) = _resolve([_entry()], HOSTS, loaded_ids=set(HOSTS))
        assert link.provenance is Provenance.DECLARED
        ips = {link.a.ip, link.b.ip}
        assert ips == {"192.168.1.11", "192.168.1.12"}

    def test_omitted_interface_single_iface_host_assumed(self):
        entry = _entry(endpoints=[{"host": "test1"}, {"host": "test4"}])
        (link,) = _resolve([entry], HOSTS, loaded_ids=set(HOSTS))
        by_host = {e.host: e for e in (link.a, link.b)}
        assert by_host["test1"].interface == "eth1"  # sole iface assumed
        assert by_host["test1"].ip == "192.168.1.11"
        assert by_host["test4"].interface is None  # no ifaces -> mgmt ip
        assert by_host["test4"].ip == "10.10.200.13"

    def test_omitted_interface_multi_iface_host_errors(self):
        entry = _entry(endpoints=[{"host": "test2"}, {"host": "test4"}])
        with pytest.raises(ValueError, match=r"ambiguous interface.*eth1.*eth2"):
            _resolve([entry], HOSTS, loaded_ids=set(HOSTS))

    def test_unknown_host_errors(self):
        entry = _entry(endpoints=[{"host": "nope"}, {"host": "test4"}])
        with pytest.raises(ValueError, match="unknown host 'nope'"):
            _resolve([entry], HOSTS, loaded_ids=set(HOSTS))

    def test_an_element_name_typed_for_a_composed_id_names_the_id_however_short(self):
        # ``a`` against ``a-qemu-3`` scores far below difflib's cutoff, so this
        # is the prefix path; ``ab`` is a sibling element and must not be offered.
        hosts = {"a-qemu-3": BARE, "ab": TEST1, "test4": BARE}
        entry = _entry(endpoints=[{"host": "a"}, {"host": "test4"}])
        with pytest.raises(ValueError, match=r"unknown host 'a'.*did you mean 'a-qemu-3'\?$"):
            _resolve([entry], hosts, loaded_ids=set(hosts))

    def test_an_old_shape_spelling_of_an_id_names_the_slugged_id(self):
        # ``BB1350_QEMU`` slugs to exactly the id ``bb1350-qemu`` (the equality
        # arm, not the ``<stem>-`` prefix), and its case plus the ``_`` score
        # under difflib's cutoff against it, so only that arm can find it.
        hosts = {"bb1350-qemu": BARE, "test4": BARE}
        entry = _entry(endpoints=[{"host": "BB1350_QEMU"}, {"host": "test4"}])
        with pytest.raises(ValueError, match=r"did you mean 'bb1350-qemu'\?$"):
            _resolve([entry], hosts, loaded_ids=set(hosts))

    def test_a_near_typo_with_no_prefix_match_still_gets_the_similarity_hint(self):
        entry = _entry(endpoints=[{"host": "tset1"}, {"host": "test4"}])
        with pytest.raises(ValueError, match=r"unknown host 'tset1'.*did you mean 'test1'"):
            _resolve([entry], HOSTS, loaded_ids=set(HOSTS))

    def test_a_name_nothing_resembles_gets_no_hint(self):
        entry = _entry(endpoints=[{"host": "zzzz"}, {"host": "test4"}])
        with pytest.raises(
            ValueError, match=r"unknown host 'zzzz' \(no such host in any lab file\)$"
        ):
            _resolve([entry], HOSTS, loaded_ids=set(HOSTS))

    def test_unknown_interface_errors(self):
        entry = _entry(endpoints=[{"host": "test1", "interface": "eth9"}, {"host": "test4"}])
        with pytest.raises(ValueError, match="no interface 'eth9'"):
            _resolve([entry], HOSTS, loaded_ids=set(HOSTS))

    def test_error_names_source_and_index(self):
        entry = _entry(endpoints=[{"host": "nope"}, {"host": "test4"}])
        with pytest.raises(ValueError, match=r"lab\.json.*index 0"):
            _resolve([entry], HOSTS, loaded_ids=set(HOSTS))

    def test_unrelated_link_skipped_not_resolved(self):
        """A link whose BOTH endpoints lie outside ``loaded_ids`` is skipped, so
        even an otherwise-broken entry (unknown host, malformed shape) cannot
        raise — containment symmetric with cross-lab host records.
        """
        bad_unknown = _entry(endpoints=[{"host": "ghost"}, {"host": "phantom"}])
        malformed = {"endpoints": [{"host": "ghost"}]}  # 1 endpoint: would fail LinkSpec
        assert _resolve([bad_unknown, malformed], HOSTS, loaded_ids={"test1"}) == []

    def test_touching_link_resolved_even_with_dangling_endpoint(self):
        """>= 1 endpoint in ``loaded_ids`` -> resolved; the other end still
        resolves from ``hosts`` even though it is outside the lab."""
        entry = _entry(endpoints=[{"host": "test1"}, {"host": "test4"}])
        (link,) = _resolve([entry], HOSTS, loaded_ids={"test1"})
        assert {link.a.host, link.b.host} == {"test1", "test4"}

    def test_a_malformed_entry_is_refused_on_one_line_without_its_values(self):
        bad = DeclaredLinkEntry(
            origin=Path("b/links.json"),
            index=0,
            entry=_entry(protocol={"token": "hunter2"}),
        )
        with pytest.raises(
            ValueError, match=r"^Invalid link in b/links\.json at index 0: protocol: "
        ) as e:
            resolve_declared_links(
                [bad], HOSTS, loaded_ids=set(HOSTS), selection=frozenset({"bench"})
            )
        assert "hunter2" not in str(e.value)
        assert "\n" not in str(e.value), "one line, not pydantic's block"

    def test_an_error_names_the_entrys_own_document_and_index(self):
        good = DeclaredLinkEntry(origin=Path("a/lab.json"), index=0, entry=_entry())
        bad = DeclaredLinkEntry(
            origin=Path("b/links.json"),
            index=3,
            entry=_entry(endpoints=[{"host": "nope"}, {"host": "test4"}]),
        )
        with pytest.raises(ValueError, match=r"^Invalid link in b/links\.json at index 3: "):
            resolve_declared_links(
                [good, bad], HOSTS, loaded_ids=set(HOSTS), selection=frozenset({"bench"})
            )


class TestLinkAttaches:
    """The one relevance decision: does a declared link join the selected labs?"""

    _SCOPE = LabScope(prefixes=frozenset({"rack-"}))

    @pytest.mark.parametrize(
        ("loaded_ids", "selection", "scope", "attaches"),
        [
            (None, None, None, True),  # nothing to narrow by: every link is offered
            (None, frozenset({"bench"}), _SCOPE, True),
            ({"test1"}, frozenset({"bench"}), None, True),
            ({"other"}, frozenset({"bench"}), None, False),  # no endpoint loaded
            ({"test1"}, frozenset({"rack-1"}), _SCOPE, True),
            ({"test1"}, frozenset({"bench"}), _SCOPE, False),  # the lab is outside the scope
            ({"test1"}, frozenset({"bench", "rack-1"}), _SCOPE, True),  # a+b: one inside
            ({"test1"}, None, _SCOPE, True),  # no lab selected: the scope narrows nothing
        ],
    )
    def test_the_rule(self, loaded_ids, selection, scope, attaches):
        assert (
            link_attaches(
                endpoint_ids=["test1", "test2"],
                selection=selection,
                loaded_ids=loaded_ids,
                scope=scope,
            )
            is attaches
        )

    def test_a_scoped_link_outside_the_selected_lab_is_skipped_unresolved(self):
        entry = DeclaredLinkEntry(
            origin=Path("lab.json"), index=0, entry={"endpoints": [{"host": "test1"}]}
        )
        assert (
            resolve_declared_links(
                [entry],
                HOSTS,
                loaded_ids=set(HOSTS),
                selection=frozenset({"bench"}),
                scope=self._SCOPE,
            )
            == []
        )


class TestImpairField:
    def test_impair_carried_onto_link(self):
        hosts = {**HOSTS, "wanem-seed": HostAddressing(ip="10.10.200.14", interfaces={})}
        entry = _entry(impair="wanem-seed")
        (link,) = _resolve([entry], hosts, loaded_ids=set(hosts))
        assert link.impair == "wanem-seed"

    def test_unknown_impair_host_rejected(self):
        entry = _entry(impair="wanem-seed")
        with pytest.raises(ValueError, match="impair host 'wanem-seed' is not a known host"):
            _resolve([entry], HOSTS, loaded_ids=set(HOSTS))

    def test_impair_host_must_not_be_an_endpoint(self):
        entry = _entry(impair="test1")
        with pytest.raises(ValueError, match="is an endpoint of the link"):
            _resolve([entry], HOSTS, loaded_ids=set(HOSTS))


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
            "guest1": _FakeHost("guest1", hop="gw", term="telnet"),
        }
        links = implicit_links(hosts)
        by_pair = {frozenset((link.a.host, link.b.host)): link for link in links}
        assert frozenset(("gw", "guest1")) in by_pair
        assert by_pair[frozenset(("gw", "guest1"))].protocol == "telnet"
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
        hosts = {"guest1": _FakeHost("guest1", hop="ghost")}
        (edge,) = [link for link in implicit_links(hosts) if "ghost" in (link.a.host, link.b.host)]
        ghost = edge.a if edge.a.host == "ghost" else edge.b
        assert ghost.ip == ""
