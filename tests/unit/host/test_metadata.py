"""Opaque metadata and lab context on runtime hosts (spec §4)."""

import pytest
from pydantic import ValidationError

from otto.host.factory import create_host_from_dict
from otto.host.lab_info import LabInfo
from otto.models.host import UnixHostSpec

_CREDS = [{"login": "u", "password": "p"}]


def test_host_metadata_rides_the_spec_to_the_host() -> None:
    host = create_host_from_dict(
        {"ip": "10.0.0.1", "element": "dut", "creds": _CREDS, "metadata": {"owner": "infra"}}
    )
    assert host.metadata == {"owner": "infra"}


def test_metadata_defaults_empty_and_is_not_shared() -> None:
    a = create_host_from_dict({"ip": "10.0.0.1", "element": "a", "creds": _CREDS})
    b = create_host_from_dict({"ip": "10.0.0.2", "element": "b", "creds": _CREDS})
    assert a.metadata == {}
    assert a.element_metadata == {}
    a.metadata["x"] = 1
    assert b.metadata == {}


def test_element_metadata_is_a_loader_argument_copied_per_host() -> None:
    shared = {"rack": "B4"}
    a = create_host_from_dict(
        {"ip": "10.0.0.1", "element": "a", "creds": _CREDS}, element_metadata=shared
    )
    b = create_host_from_dict(
        {"ip": "10.0.0.2", "element": "b", "creds": _CREDS}, element_metadata=shared
    )
    a.element_metadata["rack"] = "Z9"
    assert b.element_metadata == {"rack": "B4"}
    assert shared == {"rack": "B4"}


def test_lab_info_defaults_to_the_unattributed_lab() -> None:
    host = create_host_from_dict({"ip": "10.0.0.1", "element": "a", "creds": _CREDS})
    assert host.lab_info == LabInfo()


def test_metadata_must_be_an_object() -> None:
    # Anchored on the loc line AND the type message: a bare ``match="metadata"``
    # is also satisfied by the extra_forbidden error a spec WITHOUT the field
    # raises, so it would pass in exactly the world this test denies.
    with pytest.raises(
        ValidationError, match=r"(?m)^metadata\n\s+Input should be a valid dictionary"
    ):
        UnixHostSpec(ip="10.0.0.1", element="a", creds=_CREDS, metadata=["not", "a", "table"])


def test_hoisted_fields_are_no_longer_host_fields() -> None:
    for key, value in (("labs", ["unix"]), ("resources", ["r1"])):
        with pytest.raises(ValidationError, match=rf"{key}\s+Extra inputs are not permitted"):
            UnixHostSpec(ip="10.0.0.1", element="a", creds=_CREDS, **{key: value})
