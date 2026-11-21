"""Lab capabilities, statuses and the view adapter (lab templating #1176).

A lab source reaches the inventory view through :mod:`otto.labs.views` and
:func:`otto.labs.host_summaries_in_view`, whatever it implements: a legacy
backend keeps today's calls and receives ``default`` (or the D1 stand-in that
refuses bare references), and the refusal is one drop on every route.
"""

import json
from pathlib import Path

import pytest

from otto.examples.lab_repository import ExampleLabRepository
from otto.host.element import Element
from otto.host.factory import create_host_from_dict
from otto.host.inventory_ref import InventoryRef
from otto.inventory import (
    BareReferenceRefused,
    InventoryView,
    RefusedDefault,
    bare_refusal_message,
    resolve_host_entry,
)
from otto.labs import (
    DeclaredLinkInputs,
    Diagnostic,
    LabContribution,
    LabScope,
    LabStatus,
    SupportsHostSummaries,
    SupportsHostSummariesInView,
    SupportsInventoryView,
    SupportsLabContribution,
    SupportsLabScope,
    SupportsLabStatus,
    host_summaries,
    host_summaries_in_view,
    lab_scope_of,
    lab_statuses,
    labs_view,
    load_contribution_through,
    load_lab_through,
)
from otto.labs.drops import HostDrop, collecting_drops
from otto.labs.loader import Lab
from otto.link import DeclaredLinkEntry, HostAddressing
from otto.testing import assert_lab_repository_conforms
from tests._fixtures.labdata import built_json_source
from tests._fixtures.named_inventory import FakeInventory, FakeViewSource, LookupCounting

_ROOT = Path("/projects/bench")
_ORIGIN = "/projects/bench/.otto/settings.toml"
_CREDS = [{"login": "admin", "password": "pw"}]

D1_TEXT = bare_refusal_message(origin=_ORIGIN, key="sw-1", declared=["default", "physical"])
"""The D1 diagnostic for bare key ``sw-1`` in the refusing project below."""


def _refusing_view(counting: LookupCounting) -> InventoryView:
    """A view whose project refuses bare references, with *counting* as ``default``."""
    return InventoryView(
        inventories={"default": counting, "physical": FakeInventory({})},
        bare_refused_by={_ROOT: _ORIGIN},
    ).for_project(root=_ROOT)


_BENCH = [
    {"name": "inline-host", "hosts": [{"ip": "10.1.0.1", "creds": _CREDS}]},
    {"name": "switch", "hosts": [{"inventory": "sw-1"}]},
]
"""Lab ``bench``: an inline host beside a bare-referenced one."""

_UNIX = [{"name": "unix-host", "hosts": [{"ip": "10.1.0.9", "creds": _CREDS}]}]


class _Legacy:
    """A legacy backend: ``list_labs`` and ``load_lab`` only, resolving like the example."""

    def __init__(self, labs: dict[str, list[dict]]) -> None:
        self._labs = labs

    def load_lab(self, name, *, preferences=None, inventory=None) -> Lab:
        lab = Lab(name=name)
        for entry in self._labs[name]:
            element = Element(name=entry["name"])
            for host_data in entry["hosts"]:
                resolved = resolve_host_entry(host_data, inventory=inventory, element=element)
                lab.add_host(
                    create_host_from_dict(
                        resolved.host_data,
                        preferences=preferences,
                        lab_name=name,
                        element=element,
                        inventory_ref=resolved.ref,
                    )
                )
        return lab

    def list_labs(self) -> list[str]:
        return sorted(self._labs)


class _Recording(_Legacy):
    """A legacy backend that records the ``inventory`` each load received."""

    def __init__(self, labs) -> None:
        super().__init__(labs)
        self.received: list[object] = []

    def load_lab(self, name, *, preferences=None, inventory=None) -> Lab:
        self.received.append(inventory)
        return super().load_lab(name, preferences=preferences, inventory=inventory)


def test_a_legacy_backend_gets_all_available_statuses():
    source = _Legacy({"west": _UNIX, "east": _UNIX})
    assert not isinstance(source, SupportsLabStatus)
    statuses = lab_statuses(repository=source, view=InventoryView.of_default(inventory=None))
    assert statuses == [LabStatus(name="east"), LabStatus(name="west")]
    assert all(s.available for s in statuses)
    assert lab_scope_of(repository=source) is None


def test_a_legacy_project_forwards_default_unchanged():
    inv = FakeInventory({"sw-1": {"ip": "10.1.0.2", "creds": _CREDS}})
    source = _Recording({"unix": _UNIX})
    view = InventoryView(inventories={"default": inv, "physical": FakeInventory({})})
    load_lab_through(repository=source, name="unix", preferences=None, view=view)
    assert source.received[-1] is view.inventories["default"]
    load_lab_through(
        repository=source,
        name="unix",
        preferences=None,
        view=InventoryView.of_default(inventory=None),
    )
    assert source.received[-1] is None


def test_a_refusing_project_hands_a_legacy_backend_the_refusal():
    counting = LookupCounting({"sw-1": {"ip": "10.1.0.2", "creds": _CREDS}})
    ex = ExampleLabRepository(labs={"bench": _BENCH})
    assert not isinstance(ex, SupportsInventoryView)
    with pytest.raises(BareReferenceRefused) as e:
        load_lab_through(
            repository=ex, name="bench", preferences=None, view=_refusing_view(counting)
        )
    assert str(e.value) == D1_TEXT
    assert counting.lookups == 0


def test_the_refusal_is_one_drop_on_the_summaries_route():
    counting = LookupCounting({"sw-1": {"ip": "10.1.0.2", "creds": _CREDS}})
    ex = ExampleLabRepository(labs={"bench": _BENCH})
    assert isinstance(ex, SupportsHostSummaries)
    with collecting_drops() as drops:
        ids = [s.id for s in host_summaries_in_view(repository=ex, view=_refusing_view(counting))]
    assert ids == ["inline-host"]
    assert [d.reason for d in drops] == [D1_TEXT]
    assert [d.where for d in drops] == ["inventory reference 'sw-1'"]
    assert counting.lookups == 0


def test_the_refusal_omits_its_lab_on_the_fallback():
    counting = LookupCounting({"sw-1": {"ip": "10.1.0.2", "creds": _CREDS}})
    source = _Legacy({"bench": _BENCH, "unix": _UNIX})
    assert not isinstance(source, SupportsHostSummaries)
    with collecting_drops() as drops:
        summaries = host_summaries_in_view(repository=source, view=_refusing_view(counting))
    assert [(s.id, s.labs) for s in summaries] == [("unix-host", ["unix"])]
    assert [d.reason for d in drops] == [D1_TEXT]
    assert counting.lookups == 0


# The fallback's literals were recorded from main's ``host_summaries`` before it
# delegated (an oracle that cannot move with the change).
_FALLBACK_LABS = {
    "east": [
        {"name": "router", "hosts": [{"ip": "10.0.0.1", "creds": _CREDS}]},
        {"name": "shared", "hosts": [{"inventory": "sw-1"}]},
    ],
    "west": [
        {"name": "shared", "hosts": [{"inventory": "sw-1"}]},
        {"name": "probe", "hosts": [{"ip": "10.0.0.9", "creds": _CREDS, "os_type": "unix"}]},
    ],
    "broken": [{"name": "ghost", "hosts": [{"inventory": "missing"}]}],
}
_NO_INVENTORY = (
    "references inventory key {key!r} but no inventory is configured; declare [inventory] "
    "in ~/.otto/settings.toml or in the project's .otto/settings.toml"
)


@pytest.mark.parametrize("route", ["in_view", "legacy"])
def test_the_fallback_is_todays_for_a_legacy_project(route):
    inv = FakeInventory({"sw-1": {"ip": "10.0.0.2", "creds": _CREDS}}, label="fake:mem")
    source = _Legacy(_FALLBACK_LABS)

    def run(inventory):
        with collecting_drops() as drops:
            if route == "in_view":
                view = InventoryView.of_default(inventory=inventory)
                out = host_summaries_in_view(repository=source, view=view)
            else:
                out = host_summaries(repository=source, inventory=inventory)
        return out, drops

    out, drops = run(inv)
    assert [(s.id, s.labs, s.ip, s.os_type, [x.login for x in s.logins]) for s in out] == [
        ("probe", ["west"], "10.0.0.9", "unix", ["admin"]),
        ("router", ["east"], "10.0.0.1", "unix", ["admin"]),
        ("shared", ["east", "west"], "10.0.0.2", "unix", ["admin"]),
    ]
    assert all(not s.docker_capable and s.docker_priority == 0 for s in out)
    assert drops == [
        HostDrop(
            where="lab 'broken'",
            reason="could not load: inventory key 'missing' not found in inventory 'fake:mem'",
        )
    ]
    # The fold fills ``inventory_ref`` from the built host.
    by_id = {s.id: s for s in out}
    assert by_id["router"].inventory_ref == InventoryRef()
    shared = by_id["shared"].inventory_ref
    assert (shared.key, shared.name, shared.qualified) == ("sw-1", "default", False)

    # Without an inventory every lab holding a reference fails whole.
    out, drops = run(None)
    assert out == []
    assert [(d.where, d.reason) for d in drops] == [
        ("lab 'broken'", "could not load: " + _NO_INVENTORY.format(key="missing")),
        ("lab 'east'", "could not load: " + _NO_INVENTORY.format(key="sw-1")),
        ("lab 'west'", "could not load: " + _NO_INVENTORY.format(key="sw-1")),
    ]


def test_scope_matches_exact_names_and_literal_prefixes():
    scope = LabScope(names=frozenset({"bench"}), prefixes=frozenset({"rack-", "a.b"}))
    assert scope.contains(name="bench")
    assert scope.contains(name="rack-12")
    assert scope.contains(name="a.bc")
    assert not scope.contains(name="bench2")
    assert not scope.contains(name="axb")  # a literal prefix, not a pattern
    assert not scope.contains(name="rack")
    assert not LabScope().contains(name="bench")
    with pytest.raises(TypeError):
        LabScope(frozenset({"bench"}))  # ty: ignore[too-many-positional-arguments]


# --------------------------------------------------------------------------- #
# The value types and the view fake
# --------------------------------------------------------------------------- #


def test_a_status_is_available_without_a_diagnostic():
    down = LabStatus(name="bench", unavailable=Diagnostic(where="lab 'bench'", reason="rack moved"))
    assert not down.available
    assert LabStatus(name="bench").available
    assert HostDrop(where="x", reason="y") == HostDrop(where="x", reason="y", timed_out=False)
    assert isinstance(HostDrop(where="x", reason="y"), Diagnostic)
    with pytest.raises(TypeError):
        Diagnostic("x", "y")  # ty: ignore[too-many-positional-arguments]


def test_labs_view_records_refusals_as_drops():
    counting = LookupCounting({})
    view = labs_view(view=_refusing_view(counting))
    refused = view.bare_inventory
    assert isinstance(refused, RefusedDefault)
    with collecting_drops() as drops, pytest.raises(BareReferenceRefused):
        refused.lookup("sw-1")
    assert [d.reason for d in drops] == [D1_TEXT]


def test_the_view_fake_implements_every_capability_through_the_view():
    physical = FakeInventory({"k": {"ip": "10.2.0.1", "creds": _CREDS}})
    source = FakeViewSource(
        {"bench": [{"name": "dut", "hosts": [{"inventory": {"name": "physical", "key": "k"}}]}]},
        scope=LabScope(names=frozenset({"bench"})),
    )
    for capability in (
        SupportsInventoryView,
        SupportsHostSummariesInView,
        SupportsLabStatus,
        SupportsLabScope,
    ):
        assert isinstance(source, capability)
    view = InventoryView(inventories={"physical": physical})
    lab = load_lab_through(repository=source, name="bench", preferences=None, view=view)
    assert lab.hosts["dut"].ip == "10.2.0.1"
    (summary,) = host_summaries_in_view(repository=source, view=view)
    ref = summary.inventory_ref
    assert (summary.id, summary.ip, ref.name, ref.key, ref.qualified) == (
        "dut",
        "10.2.0.1",
        "physical",
        "k",
        True,
    )
    assert lab_statuses(repository=source, view=view) == [LabStatus(name="bench")]
    assert lab_scope_of(repository=source) == LabScope(names=frozenset({"bench"}))
    assert source.calls == {"load": 1, "summaries": 1, "status": 1, "scope": 1}


def test_a_down_view_fake_fails_status_and_load():
    source = FakeViewSource({"bench": _UNIX}, fail=RuntimeError("down"), fail_on="both")
    view = InventoryView.of_default(inventory=None)
    with pytest.raises(RuntimeError, match="down"):
        lab_statuses(repository=source, view=view)
    with pytest.raises(RuntimeError, match="down"):
        load_lab_through(repository=source, name="bench", preferences=None, view=view)
    assert source.list_labs() == ["bench"]


class _Contributing(FakeViewSource):
    """A view source with the contribution capability.

    *drop* contributes one host fewer; *stray_link* contributes a link its
    load never gives.
    """

    def __init__(self, labs, *, drop: bool = False, stray_link: bool = False) -> None:
        super().__init__(labs)
        self.drop = drop
        self.stray_link = stray_link
        self.contributions = 0

    def load_contribution(self, name, *, preferences=None, view: InventoryView) -> LabContribution:
        self.contributions += 1
        lab = self.load_lab_in_view(name, preferences=preferences, view=view)
        if self.drop:
            lab.hosts.popitem()
        entries = [
            DeclaredLinkEntry(
                origin=Path("stray.json"),
                index=0,
                entry={"endpoints": [{"host": "unix-host"}, {"host": "far"}]},
            )
        ]
        return LabContribution(
            lab=lab,
            not_found=None,
            links=DeclaredLinkInputs(
                entries=entries if self.stray_link else [],
                addressing=lambda: {"far": HostAddressing(ip="10.1.0.99")},
            ),
        )


def test_conformance_checks_contributions_only_where_implemented(tmp_path):
    """A contribution must hold the hosts its lab loads with; a source without one is not asked."""
    plain = FakeViewSource({"bench": _UNIX})
    assert not isinstance(plain, SupportsLabContribution)
    assert (
        load_contribution_through(
            repository=plain, name="bench", view=InventoryView.of_default(inventory=None)
        )
        is None
    )
    assert_lab_repository_conforms(plain, expected_labs=["bench"])

    agreeing = _Contributing({"bench": _UNIX})
    assert isinstance(agreeing, SupportsLabContribution)
    assert_lab_repository_conforms(agreeing, expected_labs=["bench"])
    assert agreeing.contributions == 1

    with pytest.raises(AssertionError, match=r"SupportsLabContribution: .*'bench'.*hosts"):
        assert_lab_repository_conforms(_Contributing({"bench": _UNIX}, drop=True))
    with pytest.raises(
        AssertionError,
        match=r"SupportsLabContribution: .*'bench'.*links.*\['far--unix-host'\] vs \[\]",
    ):
        assert_lab_repository_conforms(_Contributing({"bench": _UNIX}, stray_link=True))

    creds = [{"login": "u", "password": "p"}]
    document = {
        "labs": {"bench": {}, "other": {}},
        "elements": [
            {"name": "dut", "labs": ["bench"], "hosts": [{"ip": "10.1.0.5", "creds": creds}]},
            {"name": "peer", "labs": ["other"], "hosts": [{"ip": "10.1.0.6", "creds": creds}]},
        ],
        "links": [{"endpoints": [{"host": "dut"}, {"host": "peer"}]}],
    }
    (tmp_path / "lab.json").write_text(json.dumps(document))
    json_source = built_json_source(tmp_path, "a", tmp_path / "lab.json").repository
    assert isinstance(json_source, SupportsLabContribution)
    assert [link.id for link in json_source.load_lab("bench").links] == ["dut--peer"]
    assert_lab_repository_conforms(json_source, expected_labs=["bench"])
