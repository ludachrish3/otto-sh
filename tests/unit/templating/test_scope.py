"""A ``jsonnet`` source's contribution scope, and its links in dispatch (lab templating §6.8).

A scoped source declares the lab names it may contribute to. otto holds its
rendered output to that scope (every declared lab, every element membership
one lab name inside it), and the composite asks it only for labs inside it:
its failure spares every lab outside the scope, and its links never join one.
Every test drives real projects through one operation (``_lab_project``).
"""

import dataclasses
import re
from pathlib import Path
from typing import Any

import pytest

from otto.inventory import InventoryView
from otto.labs import (
    CompositeLabRepository,
    LabRepositoryError,
    LabScope,
    LabSource,
    LabStatusReport,
    LabUnavailableError,
)
from otto.labs.jsonnet_source import _JsonnetLabRepository
from otto.models.lab import literal_of_pattern
from otto.templating.errors import RenderCancelled
from tests._fixtures.labdata import built_json_source

from ._lab_project import CREDS, inventory, json_source, jsonnet_source, operation, project, write

PHYSICAL = {
    "edge-17": {"ip": "10.0.0.17", "creds": CREDS},
    "edge-18": {"ip": "10.0.0.18", "creds": CREDS},
}

ROUTER_SCOPE = {"prefixes": ["router-lab."]}
"""Every router lab, by prefix."""

BROKEN = "function(otto) error 'netbox down'"
"""An entry whose render fails: the source is down."""

DUT = "{ os_type: 'unix', inventory: { name: 'physical', key: 'edge-17' } }"
"""A generated host entry: the DUT ``physical/edge-17``."""


def _east(*, links: str = "[]", labs: str = r"['router-lab\\.east']") -> str:
    """An entry declaring ``router-lab.east`` with the DUT, *labs* its membership."""
    return (
        "function(otto) { labs: { 'router-lab.east': {} }, "
        f"elements: [{{ name: 'dut', labs: {labs}, hosts: [{DUT}] }}], links: {links} }}"
    )


def _lab_file(root: Path, name: str, lab: str, *, element: str = "alt1", ip: str = "10.0.0.1"):
    """Write a hand-written lab file declaring *lab* with one inline host; return its path."""
    return write(
        root / "lab_data" / f"{name}.json",
        {
            "labs": {lab: {}},
            "elements": [{"name": element, "labs": [lab], "hosts": [{"ip": ip, "creds": CREDS}]}],
        },
    )


def _project(
    root: Path,
    *,
    entry: str,
    json_files: "list[Path] | None" = None,
    json_first: bool = True,
    **source: Any,
):
    """A project with the ``physical`` inventory, *json_files* and one jsonnet source.

    The json source comes before the jsonnet one unless *json_first* is false;
    *source* is more of the jsonnet source's options.
    """
    write(root / "lab_data" / "layouts" / "lab.jsonnet", entry)
    generated = jsonnet_source(
        paths=[str(root / "lab_data" / "layouts" / "lab.jsonnet")],
        inventories=["physical"],
        **source,
    )
    hand = json_source(*json_files) if json_files else ""
    ordered = hand + generated if json_first else generated + hand
    return project(root, inventory(root, "physical", PHYSICAL) + ordered)


def _jsonnet_label(op: Any) -> str:
    [label] = [s.label for s in op.repository.sources if "/jsonnet#" in s.label]
    return label


# --------------------------------------------------------------------------- #
# A failed source fails exactly the labs it may contribute to.
# --------------------------------------------------------------------------- #


def test_failed_unscoped_source_fails_every_lab(tmp_path, otto_home):
    root = tmp_path / "sut"
    op = operation([_project(root, entry=BROKEN, json_files=[_lab_file(root, "o", "other")])])
    with pytest.raises(LabRepositoryError, match="netbox down") as caught:
        op.repository.load_lab_in_view("other", view=op.view)
    assert not isinstance(caught.value, LabUnavailableError)


def test_failed_scoped_source_spares_labs_outside_its_scope(tmp_path, otto_home):
    root = tmp_path / "sut"
    op = operation(
        [
            _project(
                root, entry=BROKEN, json_files=[_lab_file(root, "o", "other")], scope=ROUTER_SCOPE
            )
        ]
    )
    lab = op.repository.load_lab_in_view("other", view=op.view)
    assert sorted(lab.hosts) == ["alt1"]
    [outage] = op.repository.lab_status_report(view=op.view).unavailable_sources
    assert outage.scope == LabScope(prefixes=frozenset({"router-lab."}))
    assert outage.diagnostic.where == _jsonnet_label(op)
    assert "netbox down" in outage.diagnostic.reason


def test_failed_scoped_source_fails_labs_inside_with_its_reason(tmp_path, otto_home):
    root = tmp_path / "sut"
    east = _lab_file(root, "east", "router-lab.east")
    op = operation([_project(root, entry=BROKEN, json_files=[east], scope=ROUTER_SCOPE)])
    with pytest.raises(LabUnavailableError, match="netbox down") as caught:
        op.repository.load_lab_in_view("router-lab.east", view=op.view)
    assert caught.value.declared is False
    assert caught.value.diagnostic.where == _jsonnet_label(op)


def test_source_contributing_to_bench_without_declaring_it_fails_bench(tmp_path, otto_home):
    root = tmp_path / "sut"
    bench = _lab_file(root, "bench", "bench")
    contributes = (
        f"function(otto) {{ elements: [{{ name: 'dut', labs: ['bench'], hosts: [{DUT}] }}] }}"
    )
    sut = _project(root, entry=contributes, json_files=[bench], scope={"names": ["bench"]})
    healthy = operation([sut])
    assert sorted(healthy.repository.load_lab_in_view("bench", view=healthy.view).hosts) == [
        "alt1",
        "dut",
    ]
    write(root / "lab_data" / "layouts" / "lab.jsonnet", BROKEN)
    down = operation([sut])
    with pytest.raises(LabUnavailableError, match="netbox down") as caught:
        down.repository.load_lab_in_view("bench", view=down.view)
    assert caught.value.declared is False


def _entryless(root: Path, paths: "list[str]", *, scoped: bool = True):
    """A project whose jsonnet source's *paths* match no entry, beside hand-written labs.

    The json source declares ``router-lab.east`` (inside the router scope)
    and ``other`` (outside it).
    """
    east = _lab_file(root, "east", "router-lab.east")
    other = _lab_file(root, "o", "other", element="alt2", ip="10.0.0.2")
    generated = jsonnet_source(
        paths=paths, inventories=["physical"], **({"scope": ROUTER_SCOPE} if scoped else {})
    )
    return project(
        root, inventory(root, "physical", PHYSICAL) + json_source(east, other) + generated
    )


def _assert_refused_inside_spared_outside(op: Any, *, searched: str) -> None:
    """The source's no-entry failure refuses the lab inside its scope and spares the other."""
    with pytest.raises(LabUnavailableError, match="no Jsonnet entry file found") as caught:
        op.repository.load_lab_in_view("router-lab.east", view=op.view)
    assert caught.value.declared is False
    assert searched in str(caught.value)
    assert sorted(op.repository.load_lab_in_view("other", view=op.view).hosts) == ["alt2"]
    [outage] = op.repository.lab_status_report(view=op.view).unavailable_sources
    assert outage.diagnostic.where == _jsonnet_label(op)


@pytest.mark.parametrize("form", ["missing-literal", "empty-glob"])
def test_a_source_whose_paths_match_no_entry_fails_its_scope(form, tmp_path, otto_home):
    """No entry is the source's failure, never an empty contribution to a lab in its scope."""
    root = tmp_path / "sut"
    (root / "lab_data" / "layouts").mkdir(parents=True)
    path = {
        "missing-literal": root / "lab_data" / "layouts" / "absent.jsonnet",
        "empty-glob": root / "lab_data" / "layouts" / "*.jsonnet",
    }[form]
    op = operation([_entryless(root, [str(path)])])
    _assert_refused_inside_spared_outside(op, searched=str(path))


def test_entries_gone_after_a_read_fail_the_next_read(tmp_path, otto_home):
    root = tmp_path / "sut"
    entry = write(root / "lab_data" / "layouts" / "lab.jsonnet", _east())
    sut = _entryless(root, [str(entry)])
    first = operation([sut])
    assert sorted(first.repository.load_lab_in_view("router-lab.east", view=first.view).hosts) == [
        "alt1",
        "dut",
    ]
    entry.unlink()
    _assert_refused_inside_spared_outside(operation([sut]), searched=str(entry))


def test_an_unscoped_source_with_no_entry_fails_its_reads(tmp_path, otto_home):
    """Unscoped, the source may contribute to any lab, so its no-entry failure reaches each."""
    root = tmp_path / "sut"
    absent = root / "lab_data" / "layouts" / "absent.jsonnet"
    op = operation([_entryless(root, [str(absent)], scoped=False)])
    with pytest.raises(LabRepositoryError, match="no Jsonnet entry file found"):
        op.repository.load_lab_in_view("other", view=op.view)


def test_a_json_source_with_no_file_keeps_its_rule(tmp_path, otto_home):
    """A hand-written source with no lab file contributes nothing, as before."""
    root = tmp_path / "sut"
    other = _lab_file(root, "o", "other")
    absent = root / "lab_data" / "absent.json"
    op = operation([project(root, json_source(other) + json_source(absent))])
    assert op.repository.list_labs() == ["other"]
    assert sorted(op.repository.load_lab_in_view("other", view=op.view).hosts) == ["alt1"]
    assert op.repository.lab_status_report(view=op.view).unavailable_sources == []


def test_a_cancelled_scoped_read_is_its_outage_with_the_timeout_as_cause(tmp_path, otto_home):
    root = tmp_path / "sut"
    op = operation(
        [
            _project(
                root, entry=_east(), json_files=[_lab_file(root, "o", "other")], scope=ROUTER_SCOPE
            )
        ]
    )
    view = op.view
    [repository] = [s.repository for s in op.repository.sources if "/jsonnet#" in s.label]
    assert isinstance(repository, _JsonnetLabRepository)
    view.for_project(root=root).render_memo.cancel(
        scope=repository.inputs.env.project_root, reason="the completion deadline passed"
    )
    report = op.repository.lab_status_report(view=view)
    [outage] = report.unavailable_sources
    assert "the completion deadline passed" in outage.diagnostic.reason
    assert sorted(op.repository.load_lab_reported("other", view=view, report=report).hosts) == [
        "alt1"
    ]
    # A load the report did not refuse, cancelled inside the source: the cause chain holds.
    with pytest.raises(LabUnavailableError) as caught:
        op.repository.load_lab_reported(
            "router-lab.east",
            view=view,
            report=LabStatusReport(labs=[], declared_by={}, unavailable_sources=[]),
        )
    timeout = caught.value.__cause__
    assert isinstance(timeout, LabRepositoryError)
    assert isinstance(timeout.__cause__, RenderCancelled)


# --------------------------------------------------------------------------- #
# Rendered output stays inside the scope.
# --------------------------------------------------------------------------- #

OPEN_MEMBERSHIP = (
    "element 'dut': membership {pattern!r} is not one lab name, so a scoped source cannot "
    "contribute it; shared equipment that joins labs by pattern belongs in a hand-written source"
)


@pytest.mark.parametrize(
    ("labs", "pattern"),
    [(r"['router-lab\\..*']", r"router-lab\..*"), ("['router-lab.east']", "router-lab.east")],
    ids=["open-pattern", "unescaped-dot"],
)
def test_open_membership_pattern_in_a_scoped_source_is_refused(tmp_path, otto_home, labs, pattern):
    root = tmp_path / "sut"
    op = operation([_project(root, entry=_east(labs=labs), scope=ROUTER_SCOPE)])
    entry = root / "lab_data" / "layouts" / "lab.jsonnet"
    [outage] = op.repository.lab_status_report(view=op.view).unavailable_sources
    assert outage.diagnostic.reason == (
        f"Lab file '{entry}': " + OPEN_MEMBERSHIP.format(pattern=pattern)
    )
    with pytest.raises(LabUnavailableError, match="is not one lab name"):
        op.repository.load_lab_in_view("router-lab.east", view=op.view)


def test_the_same_pattern_in_an_unscoped_source_loads(tmp_path, otto_home):
    root = tmp_path / "sut"
    op = operation([_project(root, entry=_east(labs=r"['router-lab\\..*']"))])
    assert sorted(op.repository.load_lab_in_view("router-lab.east", view=op.view).hosts) == ["dut"]


def test_a_membership_naming_a_lab_outside_the_scope_is_refused(tmp_path, otto_home):
    root = tmp_path / "sut"
    labs = r"['router-lab\\.east', 'bench']"
    op = operation([_project(root, entry=_east(labs=labs), scope=ROUTER_SCOPE)])
    entry = root / "lab_data" / "layouts" / "lab.jsonnet"
    [outage] = op.repository.lab_status_report(view=op.view).unavailable_sources
    assert outage.diagnostic.reason == (
        f"Lab file '{entry}': element 'dut': membership 'bench' names lab 'bench', which is "
        "outside the source's scope (prefixes 'router-lab.')"
    )


def test_a_declared_lab_outside_the_scope_is_refused(tmp_path, otto_home):
    root = tmp_path / "sut"
    entry_text = "function(otto) { labs: { bench: {}, 'router-lab.east': {} } }"
    op = operation([_project(root, entry=entry_text, scope={"names": ["router-lab.east"]})])
    entry = root / "lab_data" / "layouts" / "lab.jsonnet"
    [outage] = op.repository.lab_status_report(view=op.view).unavailable_sources
    assert outage.diagnostic.reason == (
        f"Lab file '{entry}': lab 'bench' is outside the source's scope (names 'router-lab.east')"
    )


def test_a_scoped_source_inside_its_scope_loads(tmp_path, otto_home):
    root = tmp_path / "sut"
    op = operation([_project(root, entry=_east(), scope=ROUTER_SCOPE)])
    report = op.repository.lab_status_report(view=op.view)
    assert report.unavailable_sources == []
    lab = op.repository.load_lab_reported("router-lab.east", view=op.view, report=report)
    assert sorted(lab.hosts) == ["dut"]


@pytest.mark.parametrize(
    "json_first", [True, False], ids=["json-then-jsonnet", "jsonnet-then-json"]
)
def test_unavailable_lab_both_declaration_orders(tmp_path, otto_home, json_first):
    root = tmp_path / "sut"
    east = _lab_file(root, "east", "router-lab.east")
    down = "function(otto) { labs: { 'router-lab.east': { unavailable: 'rack powered off' } } }"
    op = operation(
        [_project(root, entry=down, json_files=[east], json_first=json_first, scope=ROUTER_SCOPE)]
    )
    if json_first:  # the jsonnet source declares last: its entry wins wholesale
        with pytest.raises(LabUnavailableError, match="rack powered off") as caught:
            op.repository.load_lab_in_view("router-lab.east", view=op.view)
        assert caught.value.declared is True
    else:
        lab = op.repository.load_lab_in_view("router-lab.east", view=op.view)
        assert sorted(lab.hosts) == ["alt1"]


# --------------------------------------------------------------------------- #
# Scoped links in dispatch.
# --------------------------------------------------------------------------- #


def test_scoped_link_never_joins_an_outside_lab_in_dispatch(tmp_path, otto_home):
    # A shared console server in ``bench``, a lab outside the scope; the
    # scoped source's DUT links to it from ``router-lab.east``.
    root = tmp_path / "sut"
    bench = _lab_file(root, "bench", "bench", element="consrv1", ip="10.0.9.1")
    link = "[{ name: 'dut-console', endpoints: [{ host: 'dut' }, { host: 'consrv1' }] }]"
    op = operation(
        [_project(root, entry=_east(links=link), json_files=[bench], scope=ROUTER_SCOPE)]
    )
    east = op.repository.load_lab_in_view("router-lab.east", view=op.view)
    [joined] = east.links
    assert (joined.name, joined.a.host, joined.b.host, joined.b.ip) == (
        "dut-console",
        "dut",
        "consrv1",
        "10.0.9.1",
    )
    # The console server is a host of bench, so only the scope admission
    # keeps the link out: the scoped source is never asked for bench.
    shared = op.repository.load_lab_in_view("bench", view=op.view)
    assert sorted(shared.hosts) == ["consrv1"]
    assert shared.links == []


class _Scoped:
    """A json source behind a scope, with the contribution capability (*capable*) or without."""

    def __init__(self, repository: Any, *, scope: LabScope, capable: bool) -> None:
        self._repository = repository
        self._scope = scope
        if capable:
            self.load_contribution = repository.load_contribution

    def lab_scope(self) -> LabScope:
        return self._scope

    def list_labs(self) -> list[str]:
        return self._repository.list_labs()

    def list_lab_status(self, *, view: InventoryView) -> Any:
        return self._repository.list_lab_status(view=view)

    def load_lab(self, name: str, *, preferences=None, inventory=None) -> Any:
        return self._repository.load_lab(name, preferences=preferences, inventory=inventory)

    def load_lab_in_view(self, name: str, *, preferences=None, view: InventoryView) -> Any:
        return self._repository.load_lab_in_view(name, preferences=preferences, view=view)


@pytest.mark.parametrize("capable", [True, False], ids=["capable", "legacy"])
def test_a_scoped_sources_link_failure_is_one_class_capable_or_legacy(tmp_path, capable):
    lab_file = write(
        tmp_path / "lab.json",
        {
            "labs": {"bench": {}},
            "elements": [
                {"name": "dut", "labs": ["bench"], "hosts": [{"ip": "10.0.0.1", "creds": CREDS}]}
            ],
            "links": [{"endpoints": [{"host": "dut"}, {"host": "ghost"}]}],
        },
    )
    source = built_json_source(tmp_path, "sut", lab_file)
    scoped = _Scoped(source.repository, scope=LabScope(names=frozenset({"bench"})), capable=capable)
    composite = CompositeLabRepository([LabSource("r/scoped", scoped)])
    with pytest.raises(LabRepositoryError) as caught:
        composite.load_lab_in_view("bench", view=InventoryView.of_default(inventory=None))
    assert type(caught.value) is LabUnavailableError
    assert caught.value.declared is False
    assert str(caught.value) == (
        "Lab 'bench' is unavailable: source r/scoped failed: Invalid link in "
        f"{lab_file} at index 0: unknown host 'ghost' (no such host in any lab file)"
    )


def test_another_scoped_sources_addressing_failure_is_that_sources(tmp_path):
    # Source y's link needs an endpoint only source x addresses, and x's
    # addressing fails: the outage is x's, never y's.
    def lab_file(name: str, document: dict[str, Any]) -> Path:
        return write(tmp_path / name / "lab.json", document)

    host = {"ip": "10.0.0.1", "creds": CREDS}
    linked = lab_file(
        "y",
        {
            "labs": {"bench": {}},
            "elements": [{"name": "dut", "labs": ["bench"], "hosts": [host]}],
            "links": [{"endpoints": [{"host": "dut"}, {"host": "ghost"}]}],
        },
    )
    ghost = lab_file("x", {"elements": [{"name": "ghost", "labs": ["other"], "hosts": [host]}]})
    bench = LabScope(names=frozenset({"bench"}))
    y = _Scoped(
        built_json_source(tmp_path / "y", "y", linked).repository, scope=bench, capable=True
    )
    x = _Scoped(built_json_source(tmp_path / "x", "x", ghost).repository, scope=bench, capable=True)
    contribution = x.load_contribution

    def failing_addressing(name: str, **kw: Any) -> Any:
        got = contribution(name, **kw)

        def down() -> Any:
            raise LabRepositoryError("addressing db down")

        return dataclasses.replace(got, links=dataclasses.replace(got.links, addressing=down))

    x.load_contribution = failing_addressing
    composite = CompositeLabRepository([LabSource("r/y", y), LabSource("r/x", x)])
    with pytest.raises(LabUnavailableError) as caught:
        composite.load_lab_in_view("bench", view=InventoryView.of_default(inventory=None))
    assert caught.value.diagnostic.where == "r/x"
    assert str(caught.value) == (
        "Lab 'bench' is unavailable: source r/x failed: addressing db down"
    )


# --------------------------------------------------------------------------- #
# The pieces.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("pattern", "literal"),
    [
        ("bench", "bench"),
        (r"router\-lab\.east\-r7", "router-lab.east-r7"),
        ("router-lab", "router-lab"),  # an unescaped hyphen is literal outside a class
        ("", ""),
        (r"router-lab\..*", None),
        ("router-lab.east", None),
        ("bench|rig", None),
        ("[b]ench", None),
        ("(bench)", None),
        ("bench?", None),
        (r"bench\d", None),
        (r"bench\1", None),
        ("^bench$", None),
        ("bench\\", None),
    ],
)
def test_literal_of_pattern_is_the_one_name_a_pattern_provably_matches(pattern, literal):
    assert literal_of_pattern(pattern) == literal
    if literal is not None:
        assert re.fullmatch(pattern, literal)


def test_literal_of_pattern_reads_every_escaped_name_back():
    names = ["".join(chr(c) for c in range(32, 127)), "east R7\t#1", "café-ü"]
    for name in names:
        assert literal_of_pattern(re.escape(name)) == name


def test_a_jsonnet_source_reports_its_scope_and_overrides(tmp_path, otto_home):
    root = tmp_path / "sut"
    scoped = operation(
        [_project(root, entry=_east(), scope=ROUTER_SCOPE, overrides=["Edge 17", "rig"])]
    )
    [repository] = [s.repository for s in scoped.repository.sources]
    assert isinstance(repository, _JsonnetLabRepository)
    assert repository.lab_scope() == LabScope(prefixes=frozenset({"router-lab."}))
    # Each entry is read as the element's identity, its slug.
    assert repository.declared_overrides() == frozenset({"edge-17", "rig"})
    other = tmp_path / "other"
    unscoped = operation([_project(other, entry=_east())])
    [plain] = [s.repository for s in unscoped.repository.sources]
    assert isinstance(plain, _JsonnetLabRepository)
    assert plain.lab_scope() is None
    assert plain.declared_overrides() == frozenset()
