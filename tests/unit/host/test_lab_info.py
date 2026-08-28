"""``LabInfo`` — the resolved lab a host carries."""

import dataclasses

import pytest

from otto.host.lab_info import LabInfo


def test_default_is_the_unattributed_lab() -> None:
    info = LabInfo()
    assert info.name == ""
    assert info.resources == frozenset()
    assert info.metadata == {}


def test_frozen() -> None:
    info = LabInfo(name="unix", resources=frozenset({"unix-bed"}), metadata={"rack": "B4"})
    with pytest.raises(dataclasses.FrozenInstanceError):
        info.name = "other"  # type: ignore[misc]


def test_metadata_is_a_per_host_copy() -> None:
    """``frozen=True`` stops rebinding, not mutation — the dict must not be shared.

    Task 5 stamps one ``lab.metadata`` table onto every host of a lab, so an
    aliased dict would let any host mutate the lab's metadata and every
    sibling's with it.
    """
    source = {"rack": "B4"}
    first = LabInfo(name="a", metadata=source)
    second = LabInfo(name="b", metadata=source)

    first.metadata["rack"] = "MUTATED"

    assert second.metadata == {"rack": "B4"}
    assert source == {"rack": "B4"}
