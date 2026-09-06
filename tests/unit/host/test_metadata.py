"""Opaque metadata and lab context on runtime hosts (spec §4)."""

import pytest
from pydantic import ValidationError

from otto.host.element import Element
from otto.host.factory import create_host_from_dict
from otto.host.lab_info import LabInfo
from otto.models.host import UnixHostSpec

_CREDS = [{"login": "u", "password": "p"}]


def test_host_metadata_rides_the_spec_to_the_host() -> None:
    host = create_host_from_dict(
        {"ip": "10.0.0.1", "creds": _CREDS, "metadata": {"owner": "infra"}}, element=Element("dut")
    )
    assert host.metadata == {"owner": "infra"}


def test_metadata_defaults_empty_and_is_not_shared() -> None:
    a = create_host_from_dict({"ip": "10.0.0.1", "creds": _CREDS}, element=Element("a"))
    b = create_host_from_dict({"ip": "10.0.0.2", "creds": _CREDS}, element=Element("b"))
    assert a.metadata == {}
    assert a.element.metadata == {}
    a.metadata["x"] = 1
    assert b.metadata == {}


def test_element_metadata_rides_on_the_element_and_is_copied_from_the_caller() -> None:
    """Two elements built from one source table do not share it.

    ``Element`` copies ``metadata`` on construction, so the loader's table is
    the file layer's alone and a host of one element cannot reach into another
    element's data through it.
    """
    shared = {"rack": "B4"}
    a = create_host_from_dict(
        {"ip": "10.0.0.1", "creds": _CREDS}, element=Element("a", metadata=shared)
    )
    b = create_host_from_dict(
        {"ip": "10.0.0.2", "creds": _CREDS}, element=Element("b", metadata=shared)
    )
    a.element.metadata["rack"] = "Z9"
    assert b.element.metadata == {"rack": "B4"}
    assert shared == {"rack": "B4"}


def test_lab_info_defaults_to_the_unattributed_lab() -> None:
    host = create_host_from_dict({"ip": "10.0.0.1", "creds": _CREDS}, element=Element("a"))
    assert host.lab_info == LabInfo()


def test_metadata_must_be_an_object() -> None:
    # Anchored on the loc line AND the type message: a bare ``match="metadata"``
    # is also satisfied by the extra_forbidden error a spec WITHOUT the field
    # raises, so it would pass in exactly the world this test denies.
    with pytest.raises(
        ValidationError, match=r"(?m)^metadata\n\s+Input should be a valid dictionary"
    ):
        UnixHostSpec(ip="10.0.0.1", creds=_CREDS, metadata=["not", "a", "table"])


def test_labs_is_no_longer_a_host_field() -> None:
    """Membership is the element's; ``labs`` on an entry is an error.

    ``resources`` is deliberately NOT checked beside it any more: it came back
    to ``HostSpec`` with spec 2026-08-28 three-level-reservations (a host may
    declare its slot), so asserting it is refused would pin the opposite of
    the rule.
    """
    with pytest.raises(ValidationError, match=r"labs\s+Extra inputs are not permitted"):
        UnixHostSpec(ip="10.0.0.1", creds=_CREDS, labs=["unix"])
