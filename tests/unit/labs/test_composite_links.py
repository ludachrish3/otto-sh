"""Declared links resolve once, after composition, across sources (lab templating D5).

A source with :class:`~otto.labs.protocol.SupportsLabContribution` hands the
composite its hosts and its links unresolved, and
:func:`~otto.labs.composition.compose_links` resolves every source's links
over the composed lab: a link may name a host another source declares, an
error names the link's own file and index, and an existing configuration's
links resolve exactly as before (pre-flight C15). A source loaded directly
still resolves against itself alone.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from otto.host.element import Element
from otto.host.factory import create_host_from_dict
from otto.host.os_profile import OsProfile, ProfileContext, ProfileFields
from otto.inventory import InventoryView
from otto.labs import (
    CompositeLabRepository,
    LabNotFoundError,
    LabRepositoryError,
    LabSource,
    json_repository,
)
from otto.labs.json_repository import _DocumentLabRepository, link_inputs_of
from otto.link.derive import addressing_from_dict, addressing_of_host
from otto.link.model import Link, LinkEndpoint, Provenance
from otto.registry import FrozenMap
from tests._fixtures.labdata import built_json_source
from tests._fixtures.named_inventory import FakeViewSource, LinkedViewSource

LAB = "bench"
_CREDS = [{"login": "u", "password": "p"}]


# --------------------------------------------------------------------------- #
# Builders.
# --------------------------------------------------------------------------- #


def _host(ip: str, **fields: Any) -> dict[str, Any]:
    return {"ip": ip, "os_type": "unix", "creds": _CREDS, **fields}


def _element(name: str, *hosts: dict[str, Any], labs: "list[str] | None" = None) -> dict[str, Any]:
    return {"name": name, "labs": labs or [LAB], "hosts": list(hosts)}


def _link(a: str, b: str, *, name: "str | None" = None, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"endpoints": [{"host": a}, {"host": b}], **extra}
    if name is not None:
        entry["name"] = name
    return entry


def _write(path: Path, **sections: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sections))
    return path


CONSOLE_SERVER_ONLY = {
    "labs": {LAB: {}},
    "elements": [_element("consrv1", _host("10.50.0.1"))],
}
"""Source A: the lab's declaration and a shared console server, no links."""

DUT_WITH_LINK_TO_CONSERV = {
    "elements": [_element("dut", _host("10.50.0.2"))],
    "links": [_link("dut", "consrv1", name="dut-consrv1")],
}
"""Source B: a DUT and its link to the console server only source A holds."""


def _view() -> InventoryView:
    return InventoryView.of_default(inventory=None)


def two_sources(root: Path, *, a: dict[str, Any], b: dict[str, Any], **kw: Any) -> list[LabSource]:
    """Two json sources, A then B, each one ``lab.json`` under *root*."""
    return [
        built_json_source(root, "a", _write(root / "a" / "lab.json", **a), **kw),
        built_json_source(root, "b", _write(root / "b" / "lab.json", **b), **kw),
    ]


def load(sources: list[LabSource], name: str = LAB) -> Any:
    """Load lab *name* through the composite over *sources*, as ``otto -l`` does."""
    composite = CompositeLabRepository(sources)
    view = _view()
    return composite.load_lab_reported(
        name, view=view, report=composite.lab_status_report(view=view)
    )


def direct_json_load(root: Path, name: str = LAB) -> Any:
    """Load lab *name* from the one json source reading ``<root>/lab.json``, no composite."""
    return built_json_source(root, "solo", root / "lab.json").repository.load_lab(name)


def _counting(monkeypatch: pytest.MonkeyPatch, owner: Any, attribute: str) -> list[object]:
    """Wrap *owner*'s *attribute*; return the list each call appends its arguments to."""
    real = getattr(owner, attribute)
    calls: list[object] = []

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, attribute, wrapper)
    return calls


def _boom(*args: Any, **kwargs: Any) -> None:
    raise AssertionError("a host was built")


def _ids(lab: Any) -> set[str]:
    return {link.id for link in lab.links}


# --------------------------------------------------------------------------- #
# Composition across sources.
# --------------------------------------------------------------------------- #


def test_a_link_reaches_a_console_server_from_another_source(tmp_path):
    lab = load(two_sources(tmp_path, a=CONSOLE_SERVER_ONLY, b=DUT_WITH_LINK_TO_CONSERV))
    assert _ids(lab) == {"dut-consrv1"}
    (link,) = lab.links
    assert {(link.a.host, link.a.ip), (link.b.host, link.b.ip)} == {
        ("dut", "10.50.0.2"),
        ("consrv1", "10.50.0.1"),
    }


@pytest.mark.parametrize("route", ["composite", "direct"])
def test_a_loaded_lab_is_logged_once_with_its_host_count(caplog, tmp_path, route):
    caplog.set_level("DEBUG", logger="otto")
    if route == "composite":
        load(two_sources(tmp_path, a=CONSOLE_SERVER_ONLY, b={"elements": []}))
    else:
        _write(tmp_path / "lab.json", **CONSOLE_SERVER_ONLY)
        direct_json_load(tmp_path)
    loaded = [r.getMessage() for r in caplog.records if "Loaded lab" in r.getMessage()]
    assert loaded == [f"Loaded lab '{LAB}' with 1 hosts"]


def test_the_composite_never_resolves_a_source_alone(monkeypatch, tmp_path):
    calls = _counting(monkeypatch, _DocumentLabRepository, "load_lab_in_view")
    load(two_sources(tmp_path, a=CONSOLE_SERVER_ONLY, b=DUT_WITH_LINK_TO_CONSERV))
    assert calls == []


def test_a_link_error_names_its_own_file_and_index(tmp_path):
    b = {
        "elements": [_element("dut", _host("10.50.0.2"))],
        "links": [_link("dut", "consrv1"), _link("dut", "ghost")],
    }
    with pytest.raises(
        LabRepositoryError,
        match=r"^Invalid link in .*/b/lab\.json at index 1: unknown host 'ghost'",
    ):
        load(two_sources(tmp_path, a=CONSOLE_SERVER_ONLY, b=b))


def test_a_broken_link_touching_the_lab_through_another_sources_host_fails(tmp_path):
    """Composed over the merged lab, B's link to A's console server now attaches, and fails.

    Each source resolving alone, B's link touched none of B's hosts and was
    skipped; the same B loaded directly still skips it.
    """
    b = {
        "elements": [_element("dut", _host("10.50.0.2")), _element("dut2", _host("10.50.0.3"))],
        "links": [_link("dut", "dut2"), _link("consrv1", "ghost")],
    }
    with pytest.raises(
        LabRepositoryError,
        match=r"^Invalid link in .*/b/lab\.json at index 1: unknown host 'ghost'",
    ):
        load(two_sources(tmp_path, a=CONSOLE_SERVER_ONLY, b=b))
    _write(tmp_path / "solo" / "lab.json", labs={LAB: {}}, **b)
    assert _ids(direct_json_load(tmp_path / "solo")) == {"dut--dut2"}, "positive control"


def test_a_broken_link_in_a_link_only_source_fails(tmp_path):
    """A source holding only links joins the composition, so its broken link fails the load."""
    a = {"labs": {LAB: {}}, "elements": [_element("alpha", _host("10.50.1.1"))]}
    b = {"links": [_link("alpha", "ghost")]}
    with pytest.raises(
        LabRepositoryError,
        match=r"^Invalid link in .*/b/lab\.json at index 0: unknown host 'ghost'",
    ):
        load(two_sources(tmp_path, a=a, b=b))


def test_link_only_source_contributes(tmp_path):
    a = {
        "labs": {LAB: {}},
        "elements": [_element("alpha", _host("10.50.1.1")), _element("beta", _host("10.50.1.2"))],
    }
    lab = load(two_sources(tmp_path, a=a, b={"links": [_link("alpha", "beta", name="extra")]}))
    assert _ids(lab) == {"extra"}


def test_a_link_only_source_still_names_why_it_holds_no_hosts(tmp_path):
    """Its not-found text stays in the composite's failure message, as before."""
    sources = two_sources(
        tmp_path,
        a={"elements": [_element("alpha", _host("10.50.1.1"))]},
        b={"links": [_link("alpha", "beta")]},
    )
    with pytest.raises(LabNotFoundError, match=r"(?s)b/json#1: Lab 'bench' is neither declared"):
        load(sources)


def test_link_to_a_slot_an_override_removed_keeps_todays_result(tmp_path):
    """A's links resolve through A's own addressing, even to slots B's override removed.

    Golden (g)'s case, plus a link both of whose ends B removed: each source
    resolved its own links before the merge, so both links stood, dangling.
    """
    a = {
        "labs": {LAB: {}},
        "elements": [
            _element(
                "chassis",
                _host("10.50.2.1", board="lc", slot=1),
                _host("10.50.2.2", board="lc", slot=2),
                _host("10.50.2.3", board="lc", slot=3),
            ),
            _element("peer", _host("10.50.2.9")),
        ],
        "links": [
            _link("chassis-lc-2", "peer"),
            _link("chassis-lc-2", "chassis-lc-3", name="backplane"),
        ],
    }
    b = {"elements": [_element("chassis", _host("10.50.2.1", board="lc", slot=1))]}
    lab = load(two_sources(tmp_path, a=a, b=b))
    assert sorted(lab.hosts) == ["chassis-lc-1", "peer"]
    assert [(link.id, link.a.ip, link.b.ip) for link in lab.links] == [
        ("chassis-lc-2--peer", "10.50.2.2", "10.50.2.9"),
        ("backplane", "10.50.2.2", "10.50.2.3"),
    ]


def test_the_declaring_sources_own_address_beats_a_later_override(tmp_path):
    """Step 1 before step 2: B's override moves ``alpha``; A's link keeps A's address, as today."""
    a = {
        "labs": {LAB: {}},
        "elements": [_element("alpha", _host("10.0.1.1")), _element("peer", _host("10.0.1.2"))],
        "links": [_link("alpha", "peer")],
    }
    b = {"elements": [_element("alpha", _host("10.0.1.99"))]}
    lab = load(two_sources(tmp_path, a=a, b=b))
    assert lab.hosts["alpha"].ip == "10.0.1.99", "positive control: B's override won the host"
    (link,) = lab.links
    assert {e.host: e.ip for e in (link.a, link.b)} == {"alpha": "10.0.1.1", "peer": "10.0.1.2"}


def test_an_endpoint_no_lab_holds_resolves_through_the_last_source(tmp_path):
    """Step 3: ``far`` is in no source's ``bench``; the last source addressing it wins."""
    root = tmp_path
    sources = [
        built_json_source(
            root,
            "a",
            _write(
                root / "a" / "lab.json",
                labs={LAB: {}},
                elements=[
                    _element("alpha", _host("10.0.0.1")),
                    _element("far", _host("10.0.0.9"), labs=["other"]),
                ],
            ),
        ),
        built_json_source(
            root, "b", _write(root / "b" / "lab.json", links=[_link("alpha", "far")])
        ),
        built_json_source(
            root,
            "c",
            _write(
                root / "c" / "lab.json",
                elements=[_element("far", _host("10.0.0.99"), labs=["zzz"])],
            ),
        ),
    ]
    lab = load(sources)
    (link,) = lab.links
    assert {e.host: e.ip for e in (link.a, link.b)} == {"alpha": "10.0.0.1", "far": "10.0.0.99"}


def test_composition_calls_each_sources_addressing_once(tmp_path):
    """The composition memoizes, so a thunk that does not cache is still called once per load."""
    from otto.labs import DeclaredLinkInputs
    from otto.labs.composition import SourceContribution, compose_links
    from otto.labs.loader import Lab
    from otto.link import DeclaredLinkEntry, HostAddressing

    calls: list[str] = []

    def addressing() -> dict[str, HostAddressing]:
        calls.append("called")
        return {"far": HostAddressing(ip="10.0.2.9")}

    lab = Lab(name=LAB)
    lab.add_host(
        create_host_from_dict(_host("10.0.2.1"), lab_name=LAB, element=Element(name="near"))
    )
    entries = [
        DeclaredLinkEntry(origin=tmp_path / "lab.json", index=i, entry=_link("near", "far", name=n))
        for i, n in enumerate(["one", "two", "three"])
    ]
    links = compose_links(
        lab=lab,
        contributions=[
            SourceContribution(
                label="a", inputs=DeclaredLinkInputs(entries=entries, addressing=addressing)
            )
        ],
    )
    assert [link.id for link in links] == ["one", "two", "three"]
    assert calls == ["called"]


def test_link_free_load_makes_zero_addressing_calls(monkeypatch, tmp_path):
    calls = _counting(monkeypatch, json_repository, "_addressing")
    hosts_only = {"elements": [_element("dut", _host("10.50.0.2"))]}
    load(two_sources(tmp_path / "x", a=CONSOLE_SERVER_ONLY, b=hosts_only))
    _write(tmp_path / "solo" / "lab.json", **CONSOLE_SERVER_ONLY)
    direct_json_load(tmp_path / "solo")
    assert calls == []
    # Positive control: a link that resolves reads its own source's addressing, once.
    load(two_sources(tmp_path / "y", a=CONSOLE_SERVER_ONLY, b=DUT_WITH_LINK_TO_CONSERV))
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Legacy sources: no contribution capability.
# --------------------------------------------------------------------------- #


def test_legacy_host_is_a_link_endpoint(tmp_path):
    legacy = FakeViewSource(
        {LAB: [{"name": "legacy", "hosts": [_host("10.50.3.1", interfaces={"eth1": "10.60.3.1"})]}]}
    )
    lab_file = _write(
        tmp_path / "lab.json",
        labs={LAB: {}},
        elements=[_element("alpha", _host("10.50.3.2"))],
        links=[_link("alpha", "legacy")],
    )
    lab = load([LabSource("legacy/fake", legacy), built_json_source(tmp_path, "a", lab_file)])
    (link,) = lab.links
    ends = {e.host: (e.interface, e.ip) for e in (link.a, link.b)}
    assert ends == {"alpha": (None, "10.50.3.2"), "legacy": ("eth1", "10.60.3.1")}


def _resolved(a: str, b: str, *, name: str) -> Link:
    return Link(a=LinkEndpoint(host=a), b=LinkEndpoint(host=b), name=name)


def test_legacy_source_keeps_its_route_and_links(tmp_path):
    legacy = LinkedViewSource(
        {LAB: [{"name": "gamma", "hosts": [_host("10.50.4.3")]}]},
        links=[_resolved("gamma", "nowhere", name="as-resolved")],
    )
    lab = load([LabSource("legacy/fake", legacy)])
    assert legacy.calls["load"] == 1, "loaded through load_lab_through, as before"
    assert lab.links == [_resolved("gamma", "nowhere", name="as-resolved")]


@pytest.mark.parametrize("legacy_last", [False, True])
def test_interleaved_legacy_and_json_link_overrides_follow_source_order(legacy_last, tmp_path):
    lab_file = _write(
        tmp_path / "lab.json",
        labs={LAB: {}},
        elements=[_element("alpha", _host("10.50.5.1")), _element("beta", _host("10.50.5.2"))],
        links=[_link("alpha", "beta", name="uplink")],
    )
    legacy = LabSource(
        "legacy/fake",
        LinkedViewSource(
            {LAB: [{"name": "gamma", "hosts": [_host("10.50.5.3")]}]},
            links=[_resolved("gamma", "alpha", name="uplink")],
        ),
    )
    json_source = built_json_source(tmp_path, "a", lab_file)
    lab = load([json_source, legacy] if legacy_last else [legacy, json_source])
    (uplink,) = lab.links
    assert {uplink.a.host, uplink.b.host} == (
        {"gamma", "alpha"} if legacy_last else {"alpha", "beta"}
    )


def test_addressing_of_host_equals_addressing_from_dict():
    entry = _host(
        "10.50.6.1",
        interfaces={"eth1": "10.60.6.1", "eth2": {"ip": "10.70.6.1", "subnet": "10.70.6.0/24"}},
    )
    element = Element(name="dut")
    host_id, from_dict = addressing_from_dict(entry, element)
    host = create_host_from_dict(entry, lab_name=LAB, element=element)
    assert host.id == host_id
    assert addressing_of_host(host) == from_dict


# --------------------------------------------------------------------------- #
# Direct loads: one source, resolved against itself, as today.
# --------------------------------------------------------------------------- #


TODAYS_LINKS = [
    Link(
        a=LinkEndpoint(host="alpha", interface="eth1", ip="10.60.7.1"),
        b=LinkEndpoint(host="beta", interface=None, ip="10.50.7.2"),
        protocol="udp",
        provenance=Provenance.DECLARED,
        name="data",
    ),
    Link(
        a=LinkEndpoint(host="beta", interface=None, ip="10.50.7.2"),
        b=LinkEndpoint(host="alpha", interface="eth1", ip="10.60.7.1"),
    ),
    # One file, one id twice: a direct load keeps both, as it always has.
    Link(
        a=LinkEndpoint(host="alpha", interface="eth1", ip="10.60.7.1"),
        b=LinkEndpoint(host="beta", interface=None, ip="10.50.7.2"),
    ),
]
"""What the direct load of :func:`_todays_lab` returned before links were composed."""


def _todays_lab(root: Path) -> Path:
    return _write(
        root / "lab.json",
        labs={LAB: {}},
        elements=[
            _element("alpha", _host("10.50.7.1", interfaces={"eth1": "10.60.7.1"})),
            _element("beta", _host("10.50.7.2")),
            _element("gamma", _host("10.50.7.3"), labs=["other"]),
        ],
        links=[
            _link("alpha", "beta", name="data", protocol="udp"),
            _link("beta", "alpha"),
            _link("alpha", "beta"),
            _link("gamma", "ghost"),  # another lab's broken link: skipped, as ever
        ],
    )


def test_direct_json_load_keeps_its_links(tmp_path):
    _todays_lab(tmp_path)
    assert direct_json_load(tmp_path).links == TODAYS_LINKS


def test_direct_load_of_a_cross_source_link_fails_as_today(tmp_path):
    _write(tmp_path / "lab.json", **DUT_WITH_LINK_TO_CONSERV)
    with pytest.raises(LabRepositoryError, match="unknown host 'consrv1'"):
        direct_json_load(tmp_path)


# --------------------------------------------------------------------------- #
# The source-wide, data-only link extraction.
# --------------------------------------------------------------------------- #


SEVERAL_LABS_ONE_SOURCE = {
    "labs": {LAB: {}, "rack": {}},
    "elements": [
        _element("alpha", _host("10.50.8.1")),
        _element("beta", _host("10.50.8.2")),
        _element("gamma", _host("10.50.8.3"), labs=["rack"]),
        _element("delta", _host("10.50.8.4"), labs=["rack"]),
    ],
    "links": [
        _link("alpha", "beta", name="bench-l1"),
        _link("gamma", "delta", name="rack-l1"),
        _link("delta", "gamma", name="rack-l2"),
    ],
}


def _repository(root: Path, document: dict[str, Any], **kw: Any) -> Any:
    return built_json_source(root, "a", _write(root / "lab.json", **document), **kw).repository


def _link_name(entry: Any) -> str:
    return entry.entry["name"]


def test_link_extraction_builds_no_host(monkeypatch, tmp_path):
    monkeypatch.setattr("otto.labs.json_repository._add_host", _boom)
    entries = _repository(tmp_path, SEVERAL_LABS_ONE_SOURCE).declared_link_entries(view=_view())
    assert {_link_name(e) for e in entries} == {"bench-l1", "rack-l1", "rack-l2"}
    assert [(e.origin, e.index) for e in entries] == [(tmp_path / "lab.json", i) for i in range(3)]


def test_link_extraction_of_a_link_only_source(tmp_path):
    repository = _repository(tmp_path, {"links": [_link("alpha", "beta", name="extra")]})
    assert [_link_name(e) for e in repository.declared_link_entries(view=_view())] == ["extra"]


def test_contribution_links_are_source_wide(tmp_path):
    repository = _repository(tmp_path, SEVERAL_LABS_ONE_SOURCE)
    contribution = repository.load_contribution(LAB, preferences=None, view=_view())
    assert sorted(contribution.lab.hosts) == ["alpha", "beta"]
    assert contribution.lab.links == []
    assert [_link_name(e) for e in contribution.links.entries] == ["bench-l1", "rack-l1", "rack-l2"]


def _carded_profiles() -> ProfileContext:
    """A project data profile that alone supplies ``board`` and ``slot``."""
    fields = ProfileFields(defaults=FrozenMap.freeze_json(value={"board": "lc", "slot": 2}))
    return ProfileContext(
        profiles=FrozenMap(mapping={"carded": OsProfile("carded", "unix", fields)}),
        owners=FrozenMap(mapping={"carded": "sut"}),
    )


_PROFILED = {
    "labs": {LAB: {}, "other": {}},
    "elements": [
        _element("srv", _host("10.50.9.1")),
        _element("dut", {**_host("10.50.9.2"), "os_type": "carded"}, labs=["other"]),
    ],
    "links": [_link("srv", "dut-lc-2")],
}
"""Lab ``bench``'s ``srv`` linked to ``dut-lc-2``, a host of lab ``other``.

The id's board and slot come only from the source's data profile ``carded``."""


@pytest.mark.parametrize("route", ["direct", "composed"])
def test_dangling_endpoint_addressing_uses_the_sources_profiles(route, monkeypatch, tmp_path):
    profiles = _carded_profiles()
    built = create_host_from_dict(
        _PROFILED["elements"][1]["hosts"][0],
        lab_name="other",
        element=Element(name="dut"),
        profiles=profiles,
    )
    assert built.id == "dut-lc-2", "positive control: the profile shapes the id"
    source = built_json_source(
        tmp_path, "a", _write(tmp_path / "lab.json", **_PROFILED), profiles=profiles
    )
    lab = source.repository.load_lab(LAB) if route == "direct" else load([source])
    (link,) = lab.links
    assert {link.a.host, link.b.host} == {"srv", built.id}
    assert built.id not in lab.hosts, "a dangling end: the host is another lab's"

    # The extraction leg builds no host, and gives the endpoint the same id.
    monkeypatch.setattr("otto.labs.json_repository._add_host", _boom)
    repository = source.repository
    view = _view()
    inputs = link_inputs_of(
        repository._read_source(best_effort=True, view=view),
        view=view,
        profiles=repository.profiles,
    )
    assert built.id in inputs.addressing()
    assert inputs.addressing()[built.id].ip == "10.50.9.2"
