"""A generated element replacing another source's machines says so (lab templating D3).

When an element from a ``jsonnet`` source replaces an element another source
contributed, the composite compares the two elements' machines as a whole
(:func:`~otto.host.inventory_ref.same_machine`, each way). The same machines
override as today, with today's warning; different ones are refused unless
the source lists the element in ``overrides``. An element from a source
without an override policy (a hand-written ``json`` source) overrides by
today's rule, whatever it replaces.
"""

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from otto.labs import LabRepositoryError

from ._lab_project import CREDS, inventory, json_source, jsonnet_source, operation, project, write

PHYSICAL = {
    "edge-17": {"ip": "10.0.0.17", "creds": CREDS},
    "edge-18": {"ip": "10.0.0.18", "creds": CREDS},
    "edge-19": {"ip": "10.0.0.19", "creds": CREDS},
    "slot-1": {"ip": "10.0.1.1", "board": "01", "creds": CREDS},
    "slot-2": {"ip": "10.0.1.2", "board": "02", "creds": CREDS},
    "slot-3": {"ip": "10.0.1.3", "board": "03", "creds": CREDS},
}
"""The DUTs ``edge-*``, and the boards ``slot-*`` of a chassis (each record owns its board)."""


def ref(key: str, *, name: str = "physical", **fields: Any) -> dict[str, Any]:
    """A host entry naming record *key* of inventory *name*, with more *fields*."""
    return {"os_type": "unix", "inventory": {"name": name, "key": key}, **fields}


def inline(ip: str, **fields: Any) -> dict[str, Any]:
    """An inline host entry at *ip*, with more *fields*."""
    return {"os_type": "unix", "ip": ip, "creds": CREDS, **fields}


def element(name: str, *hosts: dict[str, Any]) -> dict[str, Any]:
    """An element of ``bench`` holding *hosts*."""
    return {"name": name, "labs": ["bench"], "hosts": list(hosts)}


def _json_file(root: Path, name: str, *elements: dict[str, Any], declare: bool = True) -> Path:
    """Write a hand-written lab file of *elements*, declaring ``bench`` when *declare*."""
    document: dict[str, Any] = {"elements": list(elements)}
    if declare:
        document["labs"] = {"bench": {}}
    return write(root / "lab_data" / f"{name}.json", document)


def _jsonnet_source(root: Path, *elements: dict[str, Any], **keys: Any) -> str:
    """Write an entry rendering *elements*; return its ``[[lab.sources]]`` table."""
    entry = write(
        root / "lab_data" / "layouts" / "lab.jsonnet",
        "function(otto) " + json.dumps({"elements": list(elements)}),
    )
    keys.setdefault("inventories", ["physical"])
    return jsonnet_source(paths=[str(entry)], **keys)


def _settings(root: Path, *sources: str, inventories: str = "") -> str:
    return inventory(root, "physical", PHYSICAL) + inventories + "".join(sources)


def _bench(root: Path, *sources: str, inventories: str = "") -> Any:
    """Load ``bench`` from a project at *root* whose settings hold *sources* in order."""
    op = operation([project(root, _settings(root, *sources, inventories=inventories))])
    return op, op.repository.load_lab_in_view("bench", view=op.view)


def _refused(root: Path, *sources: str, inventories: str = "") -> "tuple[Any, str]":
    """Return the operation, and the error loading ``bench`` from *sources* raises."""
    op = operation([project(root, _settings(root, *sources, inventories=inventories))])
    with pytest.raises(LabRepositoryError) as caught:
        op.repository.load_lab_in_view("bench", view=op.view)
    return op, str(caught.value)


def _over(
    root: Path, hand: "list[dict[str, Any]]", generated: "list[dict[str, Any]]", **keys: Any
) -> "tuple[str, str]":
    """Return a json source of *hand* elements then a jsonnet source of *generated* ones."""
    return json_source(_json_file(root, "bench", *hand)), _jsonnet_source(root, *generated, **keys)


def _label(op: Any, backend: str) -> str:
    [label] = [s.label for s in op.repository.sources if f"/{backend}#" in s.label]
    return label


def _machines(lab: Any) -> dict[str, str]:
    """The lab's hosts as ``{id: ip}``."""
    return {host_id: host.ip for host_id, host in lab.hosts.items()}


def _refusal(op: Any, *, key: str, old: str, new: str) -> str:
    """The refusal of the jsonnet source of *op* replacing its json source's element *key*."""
    generated, owner = _label(op, "jsonnet"), _label(op, "json")
    return (
        f"element {key!r} in lab 'bench': {generated} would replace the machines of {owner} "
        f"({old}) with ({new}); declare overrides = [{key!r}] on {generated} to mean it"
    )


# --------------------------------------------------------------------------- #
# A generated element over another source's element.
# --------------------------------------------------------------------------- #

RIG = [element("rig", ref("slot-1"), ref("slot-2"))]
"""A two-board element: ``physical/slot-1`` (board 01) and ``physical/slot-2`` (board 02)."""

ONE_BOARD = [element("rig", ref("slot-1"))]
"""The same element with only its first board."""


def test_generated_element_replacing_a_different_machine_needs_overrides(tmp_path, otto_home):
    hand, generated = [element("dut", ref("edge-17"))], [element("dut", ref("edge-18"))]
    root = tmp_path / "refused"
    op, message = _refused(root, *_over(root, hand, generated))
    assert message == _refusal(
        op, key="dut", old="10.0.0.17 as physical/edge-17", new="10.0.0.18 as physical/edge-18"
    )
    root = tmp_path / "declared"
    _, lab = _bench(root, *_over(root, hand, generated, overrides=["dut"]))
    assert _machines(lab) == {"dut": "10.0.0.18"}


@pytest.mark.parametrize(
    "replaced", [ref("edge-17"), inline("10.0.0.17")], ids=["same-record", "inline-same-address"]
)
def test_same_machine_overrides_with_todays_warning(tmp_path, otto_home, caplog, replaced):
    root = tmp_path / "sut"
    sources = _over(root, [element("dut", replaced)], [element("dut", ref("edge-17"))])
    with caplog.at_level(logging.WARNING, logger="otto.labs.composite"):
        op, lab = _bench(root, *sources)
    assert _machines(lab) == {"dut": "10.0.0.17"}
    assert lab.hosts["dut"].inventory_ref.name == "physical"
    assert (
        f"element 'dut' in lab 'bench': {_label(op, 'jsonnet')} overrides {_label(op, 'json')}"
        in caplog.messages
    )


def test_replacement_removing_a_host_needs_overrides(tmp_path, otto_home):
    root = tmp_path / "refused"
    op, message = _refused(root, *_over(root, RIG, ONE_BOARD))
    assert message == _refusal(
        op,
        key="rig",
        old="10.0.1.1 as physical/slot-1, 10.0.1.2 as physical/slot-2",
        new="10.0.1.1 as physical/slot-1",
    )
    root = tmp_path / "declared"
    _, lab = _bench(root, *_over(root, RIG, ONE_BOARD, overrides=["rig"]))
    assert _machines(lab) == {"rig-01": "10.0.1.1"}


def test_replacement_adding_a_host_needs_overrides(tmp_path, otto_home):
    root = tmp_path / "refused"
    op, message = _refused(root, *_over(root, ONE_BOARD, RIG))
    assert message == _refusal(
        op,
        key="rig",
        old="10.0.1.1 as physical/slot-1",
        new="10.0.1.1 as physical/slot-1, 10.0.1.2 as physical/slot-2",
    )
    root = tmp_path / "declared"
    _, lab = _bench(root, *_over(root, ONE_BOARD, RIG, overrides=["rig"]))
    assert _machines(lab) == {"rig-01": "10.0.1.1", "rig-02": "10.0.1.2"}


def test_changed_board_slot_ids_same_machines_is_allowed(tmp_path, otto_home):
    # A hand-written board 07 slot 1 at the address the record's board 01 has.
    root = tmp_path / "sut"
    by_hand = [element("rig", inline("10.0.1.1", board="07", slot="1"))]
    _, lab = _bench(root, *_over(root, by_hand, ONE_BOARD))
    assert _machines(lab) == {"rig-01": "10.0.1.1"}


def test_multi_host_element_compared_as_a_whole(tmp_path, otto_home):
    # The same two machines on swapped boards: per host id each one changed,
    # but the element as a whole holds the same machines.
    root = tmp_path / "swapped"
    swapped = [element("rig", inline("10.0.1.2", board="01"), inline("10.0.1.1", board="02"))]
    _, lab = _bench(root, *_over(root, swapped, RIG))
    assert _machines(lab) == {"rig-01": "10.0.1.1", "rig-02": "10.0.1.2"}
    # One machine kept and one changed: the element is a different one.
    root = tmp_path / "changed"
    changed = [element("rig", ref("slot-2"), ref("slot-3"))]
    op, message = _refused(root, *_over(root, RIG, changed))
    assert message == _refusal(
        op,
        key="rig",
        old="10.0.1.1 as physical/slot-1, 10.0.1.2 as physical/slot-2",
        new="10.0.1.2 as physical/slot-2, 10.0.1.3 as physical/slot-3",
    )


def test_two_isolated_vms_same_ip_different_inventories_fail_loud(tmp_path, otto_home):
    root = tmp_path / "sut"
    vm = {"vm": {"ip": "192.168.1.10", "creds": CREDS}}
    isolated = inventory(root, "lab-a", vm) + inventory(root, "lab-b", vm)
    sources = _over(
        root,
        [element("vm", ref("vm", name="lab-a"))],
        [element("vm", ref("vm", name="lab-b"))],
        inventories=["lab-b"],
    )
    op, message = _refused(root, *sources, inventories=isolated)
    assert message == _refusal(
        op, key="vm", old="192.168.1.10 as lab-a/vm", new="192.168.1.10 as lab-b/vm"
    )


# --------------------------------------------------------------------------- #
# Everything else is today's rule.
# --------------------------------------------------------------------------- #


def test_json_replacing_a_generated_element_is_todays_rule(tmp_path, otto_home, caplog):
    root = tmp_path / "sut"
    hand, generated = _over(
        root, [element("dut", ref("edge-17"))], [element("dut", ref("edge-18"))]
    )
    with caplog.at_level(logging.WARNING, logger="otto.labs.composite"):
        op, lab = _bench(root, generated, hand)
    assert _machines(lab) == {"dut": "10.0.0.17"}
    assert (
        f"element 'dut' in lab 'bench': {_label(op, 'json')} overrides {_label(op, 'jsonnet')}"
        in caplog.messages
    )


def test_json_to_json_override_is_unchanged(tmp_path, otto_home, caplog):
    root = tmp_path / "sut"
    first = json_source(_json_file(root, "first", element("dut", inline("10.0.0.1"))))
    second = json_source(
        _json_file(root, "second", element("dut", inline("10.0.0.2")), declare=False)
    )
    with caplog.at_level(logging.WARNING, logger="otto.labs.composite"):
        op, lab = _bench(root, first, second)
    assert _machines(lab) == {"dut": "10.0.0.2"}
    [early, late] = [s.label for s in op.repository.sources]
    assert f"element 'dut' in lab 'bench': {late} overrides {early}" in caplog.messages


@pytest.mark.parametrize("generated_first", [False, True], ids=["json-first", "jsonnet-first"])
def test_declaration_order_never_changes_a_non_overlapping_result(
    tmp_path, otto_home, caplog, generated_first
):
    root = tmp_path / "sut"
    hand, generated = _over(
        root, [element("alt1", inline("10.0.0.1"))], [element("dut", ref("edge-18"))]
    )
    with caplog.at_level(logging.WARNING, logger="otto.labs.composite"):
        _, lab = _bench(root, *((generated, hand) if generated_first else (hand, generated)))
    assert _machines(lab) == {"alt1": "10.0.0.1", "dut": "10.0.0.18"}
    assert not [m for m in caplog.messages if "overrides" in m]


@pytest.mark.parametrize(
    ("generated_first", "winner"),
    [(False, "10.0.0.18"), (True, "10.0.0.17")],
    ids=["generated-last-wins", "json-last-wins"],
)
def test_reversing_an_intentional_override_changes_only_its_winner(
    tmp_path, otto_home, generated_first, winner
):
    root = tmp_path / "sut"
    hand, generated = _over(
        root,
        [element("alt1", inline("10.0.0.1")), element("dut", ref("edge-17"))],
        [element("dut", ref("edge-18")), element("gen", ref("edge-19"))],
        overrides=["dut"],
    )
    _, lab = _bench(root, *((generated, hand) if generated_first else (hand, generated)))
    assert _machines(lab) == {"alt1": "10.0.0.1", "dut": winner, "gen": "10.0.0.19"}
