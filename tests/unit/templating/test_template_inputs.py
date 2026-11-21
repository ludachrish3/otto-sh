"""What a template reads: the public, credential-free projection of its declared inventories.

:func:`~otto.inventory.snapshot.template_inputs` reads each declared
inventory below its creds overlay, projects every record onto
:data:`~otto.inventory.snapshot.TEMPLATE_PUBLIC_FIELDS`, and stops before a
discovery once the operation is cancelled.
"""

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import pytest
from pydantic import BaseModel, Field, Strict, model_serializer

from otto.creds.json_store import _JsonCredsStore
from otto.host.specs import InterfaceSpec
from otto.inventory import (
    CredsOverlay,
    Inventory,
    InventoryError,
    InventoryView,
    discover_inventory,
    snapshot,
)
from otto.inventory.discovery import Discovery
from otto.inventory.errors import InventoryDataError
from otto.inventory.snapshot import (
    TEMPLATE_PRIVATE_FIELDS,
    TEMPLATE_PUBLIC_FIELDS,
    InstanceRole,
    public_instances,
    public_record,
    template_inputs,
)
from otto.models.base import CredSpec
from otto.models.inventory import InventoryInstance, InventoryRecord
from otto.templating.errors import RenderCancelled
from otto.templating.memo import RenderMemo, cancellation
from tests._fixtures import class_only_issubclass
from tests._fixtures.named_inventory import FakeDiscoveryInventory, FakeInventory, LookupCounting

WAIT = 10.0
"""Seconds a test waits on an event before failing rather than hanging."""


def _view(**inventories: Inventory) -> InventoryView:
    """Return a hand-built view over *inventories*, by name."""
    return InventoryView(inventories=inventories)


class _CountingStore:
    """A creds store that counts the lookups it serves, delegating to *inner*."""

    def __init__(self, inner: _JsonCredsStore) -> None:
        self.inner = inner
        self.lookups = 0

    @property
    def label(self) -> str:
        return self.inner.label

    def list_keys(self) -> "list[str] | None":
        return self.inner.list_keys()

    def lookup(self, key: str) -> list[CredSpec]:
        self.lookups += 1
        return self.inner.lookup(key)

    def fingerprint(self) -> "str | None":
        return self.inner.fingerprint()


def test_every_record_field_is_classified():
    fields = set(InventoryRecord.model_fields)
    assert not TEMPLATE_PUBLIC_FIELDS & TEMPLATE_PRIVATE_FIELDS
    unclassified = fields - TEMPLATE_PUBLIC_FIELDS - TEMPLATE_PRIVATE_FIELDS
    assert not unclassified, (
        f"InventoryRecord fields {sorted(unclassified)} are neither public nor private to "
        "templates; add each to TEMPLATE_PUBLIC_FIELDS or TEMPLATE_PRIVATE_FIELDS"
    )
    assert fields == TEMPLATE_PUBLIC_FIELDS | TEMPLATE_PRIVATE_FIELDS


class _Port(BaseModel):
    """A nested model, for a field annotation that declares one."""

    name: str


@pytest.mark.parametrize("strict", [False, True], ids=["builtin", "class-only"])
def test_a_generic_alias_annotation_is_read_as_its_container(
    monkeypatch: pytest.MonkeyPatch, strict: bool
):
    """``list[X]`` is no model: on 3.10 it is an instance of ``type``, so the origin is read first.

    ``class-only`` holds ``issubclass`` to pydantic-before-2.11's rule, so on
    Python 3.10 this fails whatever pydantic is installed unless the origin
    is read first; ``builtin`` is the installed pydantic's own answer.
    """
    class_only_issubclass.install(monkeypatch, strict=strict)
    strict_str = Annotated[str, Strict()]
    assert snapshot._declared_model(list[strict_str]) is None
    assert snapshot._declared_model(list[_Port]) is None
    assert snapshot._declared_model(dict[str, _Port]) is None
    assert snapshot._declared_model(_Port | None) is _Port
    assert snapshot._declared_model(_Port) is _Port
    assert snapshot._plain(list[strict_str], ["a", "b"]) == ["a", "b"]
    assert snapshot._plain(list[_Port], [_Port(name="p")]) == [{"name": "p"}]


def test_creds_and_extra_never_reach_inputs(tmp_path: Path):
    record = {
        "ip": "10.0.0.1",
        "creds": [{"login": "SEKRIT-2", "password": "SEKRIT-2"}],
        "extra": {"token": "SEKRIT-3"},
    }
    store_file = tmp_path / "creds.json"
    store_file.write_text(json.dumps({"edge-17": [{"login": "SEKRIT-4", "password": "SEKRIT-4"}]}))
    store = _CountingStore(_JsonCredsStore(store_file))
    overlay = CredsOverlay(FakeInventory({"edge-17": record}), store=store)
    # Read through its overlay, the inventory supplies the store's creds...
    supplied = discover_inventory(inventory=overlay).records["edge-17"].creds
    assert "SEKRIT-4" in [c.login for c in supplied]
    store.lookups = 0

    inputs = template_inputs(view=_view(lab=overlay), names=["lab"], project="p")

    # ...but the projection reads below the overlay, so the store is never asked.
    assert store.lookups == 0
    assert "SEKRIT" not in json.dumps(inputs)
    assert inputs["inventories"]["lab"] == [
        {"inventory": "lab", "key": "edge-17", "ip": "10.0.0.1"}
    ]
    direct = public_record(
        inventory="lab",
        key="edge-17",
        record=InventoryRecord.model_validate(record),
        instances=None,
    )
    assert "SEKRIT" not in json.dumps(direct)


def test_inputs_hold_stated_public_fields_sorted_by_key():
    inventory = FakeInventory(
        {
            "b": {"ip": "10.0.0.2", "is_virtual": False, "labels": {"tier": 1}},
            "a": {"ip": "10.0.0.1", "site": "lab-a", "groups": ["edge"]},
        }
    )
    inputs = template_inputs(view=_view(physical=inventory), names=["physical"], project="proj")
    assert inputs == {
        "project": "proj",
        "inventories": {
            "physical": [
                {
                    "inventory": "physical",
                    "key": "a",
                    "ip": "10.0.0.1",
                    "site": "lab-a",
                    "groups": ["edge"],
                },
                {
                    "inventory": "physical",
                    "key": "b",
                    "ip": "10.0.0.2",
                    "is_virtual": False,
                    "labels": {"tier": 1},
                },
            ]
        },
        "instances": {"physical": None},
    }


def test_instances_are_null_when_unsupported_and_empty_when_declared_empty():
    walked = FakeInventory({"k": {"ip": "10.0.0.1"}})
    empty = FakeDiscoveryInventory({"k": {"ip": "10.0.0.2"}}, instances={})
    inputs = template_inputs(
        view=_view(walked=walked, empty=empty), names=["walked", "empty"], project="p"
    )
    assert inputs["instances"] == {"walked": None, "empty": {}}
    [walked_record] = inputs["inventories"]["walked"]
    assert "instances" not in walked_record
    [empty_record] = inputs["inventories"]["empty"]
    assert empty_record["instances"] == []


def test_a_record_lists_every_instance_and_role_it_plays():
    inventory = FakeDiscoveryInventory(
        {"edge-17": {"ip": "10.0.0.1"}, "ixia-9": {"ip": "10.0.0.9"}},
        instances={
            "rig-8": {"members": ["edge-17"]},
            "rig-7": {"roles": {"dut": ["edge-17"], "tgen": ["ixia-9", "edge-17"]}},
        },
    )
    inputs = template_inputs(view=_view(lab=inventory), names=["lab"], project="p")
    edge, ixia = inputs["inventories"]["lab"]
    assert edge["instances"] == [
        {"instance": "rig-7", "role": "dut"},
        {"instance": "rig-7", "role": "tgen"},
        {"instance": "rig-8", "role": None},
    ]
    assert ixia["instances"] == [{"instance": "rig-7", "role": "tgen"}]
    assert inputs["instances"]["lab"] == {
        "rig-7": {"members": [], "roles": {"dut": ["edge-17"], "tgen": ["edge-17", "ixia-9"]}},
        "rig-8": {"members": ["edge-17"], "roles": {}},
    }
    direct = public_record(
        inventory="lab",
        key="edge-17",
        record=InventoryRecord(ip="10.0.0.1"),
        instances=[InstanceRole(instance="rig-8", role=None)],
    )
    assert direct["instances"] == [{"instance": "rig-8", "role": None}]


def test_only_declared_inventories_are_read():
    read = LookupCounting({"k": {"ip": "10.0.0.1"}})
    unread = LookupCounting({"k": {"ip": "10.0.0.2"}})
    inputs = template_inputs(view=_view(read=read, unread=unread), names=["read"], project="p")
    assert list(inputs["inventories"]) == ["read"]
    assert read.lookups == 1
    assert unread.lookups == 0
    with pytest.raises(InventoryError, match="no inventory named 'missing'"):
        template_inputs(view=_view(read=read), names=["missing"], project="p")


class _PasswordInterface(InterfaceSpec):
    """A third-party interface shape that carries a secret otto's own does not have."""

    password: str = ""


class _WiderRecord(InventoryRecord):
    """A third-party record that widens a public field and adds a private one."""

    interfaces: dict[str, _PasswordInterface] = Field(default_factory=dict)  # type: ignore[assignment]
    token: str = ""


def test_a_subclass_cannot_widen_a_public_field():
    record = _WiderRecord(
        ip="10.0.0.1",
        interfaces={"eth0": {"ip": "10.0.0.2", "password": "SEKRIT-B"}},
        token="SEKRIT-A",
    )
    projected = public_record(inventory="lab", key="k", record=record, instances=None)
    assert "SEKRIT" not in json.dumps(projected)
    assert projected["interfaces"]["eth0"]["ip"] == "10.0.0.2"
    assert "token" not in projected


UNVALIDATED = {
    "a dict in labels": InventoryRecord.model_construct(
        ip="10.0.0.1", labels={"x": {"password": "SEKRIT-D"}}, _fields_set={"ip", "labels"}
    ),
    "no ip at all": InventoryRecord.model_construct(
        labels={"note": "SEKRIT-F"}, _fields_set={"labels"}
    ),
    "a credential in groups": InventoryRecord.model_construct(
        ip="10.0.0.1",
        groups=[CredSpec(login="SEKRIT-E", password="SEKRIT-E")],
        _fields_set={"ip", "groups"},
    ),
}


@pytest.mark.parametrize("shape", sorted(UNVALIDATED))
def test_an_unvalidated_record_is_refused_without_quoting_it(shape: str, recwarn):
    record = UNVALIDATED[shape]
    with pytest.raises(InventoryDataError) as raised:
        public_record(inventory="lab", key="edge-17", record=record, instances=None)
    message = str(raised.value)
    assert "'lab'" in message
    assert "'edge-17'" in message
    assert "SEKRIT" not in message
    assert "are missing or invalid" in message
    # The schema's own error quotes the value, so it is never chained, even implicitly.
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert not [str(w.message) for w in recwarn]


class _Unvalidated:
    """A backend whose discovery hands over a record built without validation."""

    label = "fake:unvalidated"
    supplies = frozenset[str]()

    def inventory_discovery(self) -> Discovery:
        return Discovery(records={"edge-17": UNVALIDATED["a dict in labels"]})

    def lookup(self, key: str) -> InventoryRecord:
        return UNVALIDATED["a dict in labels"]

    def list_keys(self) -> list[str]:
        return ["edge-17"]

    def fingerprint(self) -> None:
        return None


def test_template_inputs_refuse_an_unvalidated_record_by_inventory_and_key():
    with pytest.raises(InventoryError, match="inventory 'lab', key 'edge-17'") as raised:
        template_inputs(view=_view(lab=_Unvalidated()), names=["lab"], project="p")
    assert "SEKRIT" not in str(raised.value)


class _PasswordInstance(InventoryInstance):
    """A third-party instance that carries a secret otto's own instance does not have."""

    password: str = ""


class _Instances:
    """A backend whose discovery hands over the instances a test gives it, as they are."""

    label = "fake:instances"
    supplies = frozenset[str]()

    def __init__(self, instances: "dict[str, InventoryInstance]") -> None:
        self._instances = instances
        self._records = {"edge-17": InventoryRecord(ip="10.0.0.1")}

    def inventory_discovery(self) -> Discovery:
        return Discovery(records=self._records, instances=self._instances)

    def lookup(self, key: str) -> InventoryRecord:
        return self._records[key]

    def list_keys(self) -> list[str]:
        return list(self._records)

    def fingerprint(self) -> None:
        return None


def test_a_subclass_instance_reaches_neither_the_inputs_nor_a_rendered_lab(tmp_path: Path):
    """Instances are projected through otto's own schema: members and roles only, validated."""
    from otto.templating.render import RenderRequest, render

    from ._render_helpers import policy

    rig = _PasswordInstance(members=["edge-17"], password="SEKRIT-I")
    inputs = template_inputs(view=_view(lab=_Instances({"rig-7": rig})), names=["lab"], project="p")
    assert "SEKRIT" not in json.dumps(inputs)
    assert inputs["instances"]["lab"] == {"rig-7": {"members": ["edge-17"], "roles": {}}}
    [edge] = inputs["inventories"]["lab"]
    assert edge["instances"] == [{"instance": "rig-7", "role": None}]
    # A template that tries every way to the field gets nothing: it is not there.
    entry = tmp_path / "e.jsonnet"
    entry.write_text(
        "function(otto) { labs: { [std.get(i, 'password', 'none') + ':' + "
        "std.join(',', std.objectFields(i))]: {} for i in std.objectValues(otto.instances.lab) } }"
    )
    text = render(RenderRequest(entry=entry, tla={"otto": inputs}, policy=policy())).text
    assert "SEKRIT" not in text
    assert json.loads(text)["labs"] == {"none:members,roles": {}}


UNVALIDATED_INSTANCES = {
    "a dict among the members": InventoryInstance.model_construct(
        members=[{"password": "SEKRIT-J"}], roles={}
    ),
    "a list for the roles": InventoryInstance.model_construct(members=[], roles=["SEKRIT-K"]),
    "a member that also plays a role": InventoryInstance.model_construct(
        members=["SEKRIT-L"], roles={"dut": ["SEKRIT-L"]}
    ),
}


@pytest.mark.parametrize("shape", sorted(UNVALIDATED_INSTANCES))
def test_an_unvalidated_instance_is_refused_without_quoting_it(shape: str, recwarn):
    instances = {"rig-7": UNVALIDATED_INSTANCES[shape]}
    with pytest.raises(InventoryDataError) as raised:
        public_instances(inventory="lab", instances=instances)
    message = str(raised.value)
    assert "inventory 'lab', instance 'rig-7'" in message
    assert "SEKRIT" not in message
    # The schema's own error quotes the value, so it is never chained, even implicitly.
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert not [str(w.message) for w in recwarn]
    with pytest.raises(InventoryError, match="instance 'rig-7'") as through:
        template_inputs(view=_view(lab=_Instances(instances)), names=["lab"], project="p")
    assert "SEKRIT" not in str(through.value)


class _SerializesASecret(BaseModel):
    """A backend's own value whose serializer answers with the secret it holds."""

    password: str = "SEKRIT-S"

    @model_serializer
    def _as_the_secret(self) -> str:
        return self.password


class _RaisesASecret(BaseModel):
    """A backend's own value whose serializer fails with the secret it holds."""

    password: str = "SEKRIT-R"

    @model_serializer
    def _as_the_secret(self) -> str:
        raise ValueError(self.password)


SECRET_VALUES = {
    "its serializer returns the secret": _SerializesASecret,
    "its serializer raises the secret": _RaisesASecret,
}

_EDGE = {"edge-17": InventoryRecord(ip="10.0.0.1")}

_Handed = tuple[dict[str, InventoryRecord], "dict[str, InventoryInstance] | None"]
"""What a backend hands over: its records, and its instances or ``None``."""

SMUGGLED: dict[str, Callable[[BaseModel], _Handed]] = {
    "an instance member": lambda v: (
        _EDGE,
        {"rig-7": InventoryInstance.model_construct(members=[v], roles={})},
    ),
    "an instance role": lambda v: (
        _EDGE,
        {"rig-7": InventoryInstance.model_construct(members=[], roles={"dut": [v]})},
    ),
    "a record group": lambda v: (
        {"edge-17": InventoryRecord.model_construct(ip="10.0.0.1", groups=[v])},
        None,
    ),
    "a record label": lambda v: (
        {"edge-17": InventoryRecord.model_construct(ip="10.0.0.1", labels={"note": v})},
        None,
    ),
    "an interface's ip": lambda v: (
        {
            "edge-17": InventoryRecord.model_construct(
                ip="10.0.0.1", interfaces={"eth0": InterfaceSpec.model_construct(ip=v)}
            )
        },
        None,
    ),
}


class _Handing:
    """A backend whose discovery hands over the records and instances a test gives, as they are."""

    label = "fake:handing"
    supplies = frozenset[str]()

    def __init__(
        self,
        records: "dict[str, InventoryRecord]",
        instances: "dict[str, InventoryInstance] | None",
    ) -> None:
        self._records = records
        self._instances = instances

    def inventory_discovery(self) -> Discovery:
        return Discovery(records=self._records, instances=self._instances)

    def lookup(self, key: str) -> InventoryRecord:
        return self._records[key]

    def list_keys(self) -> list[str]:
        return list(self._records)

    def fingerprint(self) -> None:
        return None


def _project(
    records: "dict[str, InventoryRecord]", instances: "dict[str, InventoryInstance] | None"
) -> None:
    """Project what a backend hands over: its instances when it has them, else its one record."""
    if instances is None:
        public_record(inventory="lab", key="edge-17", record=records["edge-17"], instances=None)
    else:
        public_instances(inventory="lab", instances=instances)


def _chain(error: BaseException) -> "list[BaseException]":
    """Return *error* and every error it is chained to, explicitly or implicitly."""
    seen: list[BaseException] = []
    pending: list[BaseException | None] = [error]
    while pending:
        e = pending.pop()
        if e is None or any(e is s for s in seen):
            continue
        seen.append(e)
        pending += [e.__cause__, e.__context__]
    return seen


def _assert_secret_free(error: BaseException) -> None:
    for e in _chain(error):
        assert "SEKRIT" not in str(e), e
        assert "SEKRIT" not in repr(e), e


@pytest.mark.parametrize("value", sorted(SECRET_VALUES))
@pytest.mark.parametrize("place", sorted(SMUGGLED))
def test_a_value_with_its_own_serializer_never_runs_it(
    place: str, value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recwarn
):
    """A malformed value is validated before anything is serialized, so its serializer never runs.

    Not as a lab name, not as an error: the refusal is the projection's own,
    names no value and keeps no exception, and no template is evaluated.
    """
    from otto.host.os_profile import ProfileContext
    from otto.labs import LabRepositoryError
    from otto.labs.documents import read_lab_document
    from otto.labs.jsonnet_source import JsonnetLabSourceConfig, JsonnetSourceInputs
    from otto.labs.sources import LabSourceEnv

    from ._render_helpers import catalogued_view, count_evaluations

    records, instances = SMUGGLED[place](SECRET_VALUES[value]())
    with pytest.raises(InventoryDataError) as direct:
        _project(records, instances)
    assert direct.value.__cause__ is None
    assert direct.value.__context__ is None
    _assert_secret_free(direct.value)

    backend = _Handing(records, instances)
    with pytest.raises(InventoryError) as through:
        template_inputs(view=_view(lab=backend), names=["lab"], project="p")
    _assert_secret_free(through.value)

    entry = tmp_path / "lab.jsonnet"
    entry.write_text(
        "function(otto) { labs: { [r.key + std.join(',', std.objectFields(otto.instances))]: {} "
        "for r in otto.inventories.lab } }"
    )
    env = LabSourceEnv(
        project_root=tmp_path,
        label="sut/jsonnet#1",
        origin=str(tmp_path / ".otto" / "settings.toml"),
        profiles=ProfileContext.empty(),
        project_name="sut",
    )
    config = JsonnetLabSourceConfig.model_validate(
        {"paths": [str(entry)], "inventories": ["lab"]}, context={"env": env}
    )
    render = JsonnetSourceInputs(config=config, env=env).render_inputs(
        catalogued_view(inventories={"lab": backend})
    )
    evaluations = count_evaluations(monkeypatch)
    with pytest.raises(LabRepositoryError) as rendered:
        read_lab_document(entry, render=render)
    _assert_secret_free(rendered.value)
    assert evaluations() == 0, "a template was evaluated over a refused input"
    assert not [str(w.message) for w in recwarn]


def test_a_repeated_name_is_read_once():
    a = FakeDiscoveryInventory({"a": {"ip": "10.0.0.1"}})
    inputs = template_inputs(view=_view(a=a), names=["a", "a"], project="p")
    assert a.fetches == 1
    assert list(inputs["inventories"]) == ["a"]


def test_a_single_string_is_not_a_list_of_names():
    a = FakeDiscoveryInventory({"a": {"ip": "10.0.0.1"}})
    with pytest.raises(TypeError, match="sequence of inventory names"):
        template_inputs(view=_view(a=a), names="a", project="p")
    assert a.fetches == 0


class _Blocking(FakeDiscoveryInventory):
    """A discovery that signals :attr:`started`, then waits for :attr:`release`."""

    def __init__(self) -> None:
        super().__init__({"a": {"ip": "10.0.0.1"}})
        self.started = threading.Event()
        self.release = threading.Event()

    def inventory_discovery(self) -> Discovery:
        self.started.set()
        assert self.release.wait(WAIT)
        return super().inventory_discovery()


def test_cancellation_during_inventory_a_never_starts_inventory_b(tmp_path: Path):
    memo = RenderMemo()
    a = _Blocking()
    b = FakeDiscoveryInventory({"b": {"ip": "10.0.0.2"}})
    raised: list[BaseException] = []

    def worker() -> None:
        with cancellation(memo.cancelled(tmp_path)):
            try:
                template_inputs(view=_view(a=a, b=b), names=["a", "b"], project="p")
            except BaseException as e:  # noqa: BLE001 - the test reports what the worker raised
                raised.append(e)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    assert a.started.wait(WAIT)
    memo.cancel(scope=tmp_path, reason="the completion deadline passed")
    a.release.set()
    thread.join(WAIT)
    assert not thread.is_alive()
    assert b.fetches == 0
    [error] = raised
    assert isinstance(error, RenderCancelled)
    assert "'b'" in str(error)


def test_cancellation_is_checked_before_the_first_discovery(tmp_path: Path):
    memo = RenderMemo()
    memo.cancel(scope=tmp_path, reason="the completion deadline passed")
    a = FakeDiscoveryInventory({"a": {"ip": "10.0.0.1"}})
    with cancellation(memo.cancelled(tmp_path)), pytest.raises(RenderCancelled, match="'a'"):
        template_inputs(view=_view(a=a), names=["a"], project="p")
    assert a.fetches == 0
