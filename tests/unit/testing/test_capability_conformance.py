"""The conformance suites check each optional capability on its own (lab templating #1176).

An inventory that answers discovery must agree with its own ``lookup``. A lab
source is checked through the adapters production uses
(:func:`~otto.labs.load_lab_through`, :func:`~otto.labs.host_summaries_in_view`),
by default under the view a legacy ``inventory=`` stands for, or under a
supplied named view. Every bad plugin below fails with the text of the one
rule it breaks; every good one conforms.
"""

from pathlib import Path

import pytest

from otto.examples.lab_repository import ExampleLabRepository
from otto.inventory import Discovery, InventoryView, discover_inventory, resolve_view_entry
from otto.labs import (
    CompositeLabRepository,
    Diagnostic,
    HostSummary,
    LabRepositoryError,
    LabScope,
    LabSource,
    LabStatus,
    LabUnavailableError,
)
from otto.labs.loader import Lab
from otto.models.inventory import InventoryRecord
from otto.testing import assert_inventory_conforms, assert_lab_repository_conforms
from tests._fixtures.named_inventory import (
    FakeDiscoveryInventory,
    FakeInventory,
    FakeViewSource,
    assert_same_discovery,
    json_twin,
)

_CREDS = [{"login": "u", "password": "p"}]
_QUALIFIED = {"name": "physical", "key": "k"}


def _el(name: str, host: dict) -> dict:
    """One element dict holding one host dict."""
    return {"name": name, "hosts": [{**host, "creds": _CREDS}]}


def _named_view() -> InventoryView:
    """``default`` and ``physical`` both hold ``k``, at different addresses."""
    return InventoryView(
        inventories={
            "default": FakeInventory({"k": {"ip": "10.0.0.9"}}),
            "physical": FakeInventory({"k": {"ip": "10.0.0.1"}}),
        }
    )


def _qualified_labs() -> dict[str, list[dict]]:
    return {"rig": [_el("dut", {"inventory": _QUALIFIED})]}


def test_a_discovery_that_disagrees_with_lookup_fails() -> None:
    class Disagreeing(FakeDiscoveryInventory):
        def inventory_discovery(self) -> Discovery:
            found = super().inventory_discovery()
            return Discovery(
                records={**found.records, "ghost": found.records["k"]},
                instances=found.instances,
            )

    with pytest.raises(AssertionError, match=r"SupportsDiscovery: .*record keys.*list_keys"):
        assert_inventory_conforms(Disagreeing({"k": {"ip": "10.0.0.1"}}))


def test_a_discovery_record_that_differs_from_lookup_fails() -> None:
    class Drifting(FakeDiscoveryInventory):
        def lookup(self, key):
            record = super().lookup(key)
            return record.model_copy(update={"ip": "10.9.9.9"})

    with pytest.raises(AssertionError, match=r"SupportsDiscovery: .*'k'.*equal lookup"):
        assert_inventory_conforms(Drifting({"k": {"ip": "10.0.0.1"}}))


def test_a_discovery_record_stating_more_than_lookup_fails() -> None:
    """Equal values, different stated fields: otto joins, exports and diffs them apart."""

    class OverStating(FakeDiscoveryInventory):
        def inventory_discovery(self) -> Discovery:
            super().inventory_discovery()
            stated = {"ip": "10.0.0.1", "is_virtual": False}
            return Discovery(records={"k": InventoryRecord.model_validate(stated)})

    assert OverStating({"k": {"ip": "10.0.0.1"}}).lookup("k") == InventoryRecord.model_validate(
        {"ip": "10.0.0.1", "is_virtual": False}
    )
    with pytest.raises(AssertionError, match=r"SupportsDiscovery: .*'k'.*states"):
        assert_inventory_conforms(OverStating({"k": {"ip": "10.0.0.1"}}))


def test_a_backend_that_ignores_the_view_fails() -> None:
    class Ignoring(FakeViewSource):
        """Drops the qualifier: ``physical/k`` is resolved as a bare ``k``."""

        def load_lab_in_view(self, name, *, preferences=None, view: InventoryView) -> Lab:
            def bare(host_data, element):
                ref = host_data.get("inventory")
                if isinstance(ref, dict):
                    host_data = {**host_data, "inventory": ref["key"]}
                return resolve_view_entry(host_data, view=view, element=element)

            return self._build(name, preferences=preferences, resolve=bare)

    with pytest.raises(AssertionError, match=r"under the supplied view, host 'dut'") as info:
        assert_lab_repository_conforms(
            Ignoring(_qualified_labs()), view=_named_view(), expected_ips={"dut": "10.0.0.1"}
        )
    assert "10.0.0.9" in str(info.value)


def test_summaries_in_view_that_disagree_with_loads_fail() -> None:
    class Misreporting(FakeViewSource):
        def list_host_summaries_in_view(self, *, view: InventoryView) -> list[HostSummary]:
            return [
                HostSummary(id=s.id, labs=s.labs, ip="10.7.7.7", os_type=s.os_type, logins=s.logins)
                for s in super().list_host_summaries_in_view(view=view)
            ]

    with pytest.raises(
        AssertionError, match=r"SupportsHostSummariesInView: 'dut'\.ip is '10\.7\.7\.7'"
    ):
        assert_lab_repository_conforms(Misreporting(_qualified_labs()), view=_named_view())


def test_a_status_outside_the_scope_fails() -> None:
    source = FakeViewSource(
        {"bench": [_el("a", {"ip": "10.0.0.1"})], "other": [_el("b", {"ip": "10.0.0.2"})]},
        scope=LabScope(names=frozenset({"bench"})),
    )
    with pytest.raises(AssertionError, match=r"SupportsLabScope: .*'other'.*outside") as info:
        assert_lab_repository_conforms(source)
    assert "reports lab 'bench'" not in str(info.value)


def test_an_unavailable_status_without_a_reason_fails() -> None:
    source = FakeViewSource(
        {"a": [_el("a", {"ip": "10.0.0.1"})], "b": [_el("b", {"ip": "10.0.0.2"})]},
        statuses=[
            LabStatus(name="a"),
            LabStatus(name="b", unavailable=Diagnostic(where="here", reason="")),
        ],
    )
    with pytest.raises(AssertionError, match=r"SupportsLabStatus: lab 'b' is unavailable .*reason"):
        assert_lab_repository_conforms(source)


def test_statuses_that_differ_from_list_labs_fail() -> None:
    source = FakeViewSource(
        {"a": [_el("a", {"ip": "10.0.0.1"})]},
        statuses=[LabStatus(name="a"), LabStatus(name="ghost")],
    )
    with pytest.raises(AssertionError, match=r"SupportsLabStatus: .*list_labs"):
        assert_lab_repository_conforms(source)


def test_a_view_load_that_differs_from_the_legacy_load_fails() -> None:
    class Diverging(FakeViewSource):
        def load_lab(self, name, *, preferences=None, inventory=None) -> Lab:
            lab = super().load_lab(name, preferences=preferences, inventory=inventory)
            lab.hosts.clear()
            return lab

    with pytest.raises(AssertionError, match=r"SupportsInventoryView: .*'a'.*same hosts"):
        assert_lab_repository_conforms(Diverging({"a": [_el("h", {"ip": "10.0.0.1"})]}))


def test_the_composite_with_an_unavailable_lab_conforms() -> None:
    source = FakeViewSource(
        {"up": [_el("a", {"ip": "10.0.0.1"})], "down": [_el("b", {"ip": "10.0.0.2"})]},
        statuses=[
            LabStatus(name="down", unavailable=Diagnostic(where="r/one", reason="in repair")),
            LabStatus(name="up"),
        ],
    )
    composite = CompositeLabRepository([LabSource("r/one", source)])
    assert_lab_repository_conforms(composite, expected_labs=["up", "down"])
    assert_lab_repository_conforms(composite, view=_named_view())


def test_a_referenced_legacy_plugin_conforms_with_inventory() -> None:
    repository = ExampleLabRepository(
        labs={"east": [_el("router1", {"inventory": "k"})]}, resources={}
    )
    inventory = FakeInventory({"k": {"ip": "10.0.0.1"}})
    assert_lab_repository_conforms(
        repository,
        expected_labs=["east"],
        expect_host_summaries=True,
        inventory=inventory,
        expected_ips={"router1": "10.0.0.1"},
    )
    # Without its inventory the same plugin cannot load: the binding is what conforms.
    with pytest.raises(AssertionError, match=r"load_lab\('east'\) raised"):
        assert_lab_repository_conforms(repository)


def test_a_qualified_lab_conforms_in_named_mode() -> None:
    source = FakeViewSource(_qualified_labs())
    assert_lab_repository_conforms(
        source, expected_labs=["rig"], view=_named_view(), expected_ips={"dut": "10.0.0.1"}
    )
    assert source.views["load"], "the probes loaded through the view"
    assert source.views["summaries"], "the summaries were read under the view"


def test_a_status_only_repository_conforms_in_named_mode() -> None:
    class StatusOnly:
        def __init__(self) -> None:
            self._inner = ExampleLabRepository(
                labs={"solo": [_el("box", {"ip": "10.0.0.5"})]}, resources={}
            )
            self.status_views: list[InventoryView] = []

        def list_labs(self) -> list[str]:
            return self._inner.list_labs()

        def load_lab(self, name, *, preferences=None, inventory=None) -> Lab:
            return self._inner.load_lab(name, preferences=preferences, inventory=inventory)

        def list_lab_status(self, *, view: InventoryView) -> list[LabStatus]:
            self.status_views.append(view)
            return [LabStatus(name=n) for n in self.list_labs()]

    repository = StatusOnly()
    assert_lab_repository_conforms(repository, view=_named_view(), expected_ips={"box": "10.0.0.5"})
    assert repository.status_views


def test_the_fakes_and_the_json_twin_conform(tmp_path: Path) -> None:
    fake = FakeDiscoveryInventory(
        {"k": {"ip": "10.0.0.1", "groups": ["edge"]}, "m": {"ip": "10.0.0.2"}},
        instances={"rig-7": {"members": ["m"], "roles": {"dut": ["k"]}}},
    )
    assert_inventory_conforms(fake, expected_keys=["k", "m"])
    twin = json_twin(tmp_path=tmp_path, discovery=discover_inventory(inventory=fake))
    assert_inventory_conforms(twin, expected_keys=["k", "m"])
    assert_same_discovery(left=fake, right=twin)

    assert_lab_repository_conforms(
        FakeViewSource({"a": [_el("h", {"ip": "10.0.0.1"})]}),
        expected_labs=["a"],
    )


def test_an_unknown_lab_answered_by_a_declared_unavailability_fails() -> None:
    class Declaring(FakeViewSource):
        def load_lab(self, name, *, preferences=None, inventory=None) -> Lab:
            if name not in self.labs:
                raise LabUnavailableError(
                    name=name,
                    diagnostic=Diagnostic(where="here", reason="in repair"),
                    declared=True,
                )
            return super().load_lab(name, preferences=preferences, inventory=inventory)

    with pytest.raises(AssertionError, match=r"must raise LabNotFoundError, got a declared"):
        assert_lab_repository_conforms(Declaring({"a": [_el("h", {"ip": "10.0.0.1"})]}))


def test_an_unknown_lab_inside_a_failed_scope_conforms() -> None:
    # The failed source still lists its labs; they are reported unavailable (spec §6.8).
    down = FakeViewSource(
        {"__otto_conformance_rack": [_el("d", {"ip": "10.0.0.2"})]},
        scope=LabScope(prefixes=frozenset({"__otto_conformance"})),
        fail=LabRepositoryError("db down"),
        fail_on="both",
    )
    composite = CompositeLabRepository(
        [
            LabSource("r/up", FakeViewSource({"up": [_el("a", {"ip": "10.0.0.1"})]})),
            LabSource("r/down", down),
        ]
    )
    with pytest.raises(LabUnavailableError) as info:
        composite.load_lab("__otto_conformance_no_such_lab__")
    assert info.value.declared is False
    assert_lab_repository_conforms(composite, expected_labs=["up"])


def test_the_in_view_summaries_alone_satisfy_expect_host_summaries() -> None:
    assert_lab_repository_conforms(
        FakeViewSource({"a": [_el("h", {"ip": "10.0.0.1"})]}), expect_host_summaries=True
    )
