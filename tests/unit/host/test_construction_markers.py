"""Field kinds: how each host-spec field reaches the host constructor."""

import copy
import dataclasses
from pathlib import Path
from typing import Annotated

import pytest

from otto.host import (
    Consume,
    Convert,
    EmbeddedHostSpec,
    HostSpec,
    Omit,
    Pass,
    Resolve,
    RuntimeOnly,
    UnixHostSpec,
)
from otto.host.construction import class_marker, field_kind, profile_inputs
from otto.host.element import Element
from otto.host.factory import create_host_from_dict
from otto.host.login_proxy import Cred
from otto.host.options import SnmpOptions, TelnetOptions
from otto.host.remote_host import RemoteHost
from otto.host.toolchain import Toolchain
from otto.host.unix_host import UnixHost

_CONVERT = {
    "creds",
    "power_control",
    "default_dest_dir",
    "metadata",
    "resources",
    "debug_log_globs",
    "interfaces",
    "telnet_options",
    "console_options",
    "snmp",
    "toolchain",
    "command_frame",
}
_UNIX_CONVERT = {
    "ssh_options",
    "sftp_options",
    "scp_options",
    "ftp_options",
    "nc_options",
    "userland_options",
}
_EMBEDDED_CONVERT = {"filesystem", "loader"}
_UNIX_RESOLVE = {
    "valid_terms",
    "valid_transfers",
    "valid_impairers",
    "term",
    "transfer",
    "impairer",
}
_EMBEDDED_RESOLVE = {"valid_terms", "valid_transfers", "term", "transfer"}


def _kinds(spec):
    return {n: type(field_kind(spec, n)) for n in spec.model_fields}


def test_the_conversions_are_convert():
    unix, embedded = _kinds(UnixHostSpec), _kinds(EmbeddedHostSpec)
    assert {n for n, k in unix.items() if k is Convert} == _CONVERT | _UNIX_CONVERT
    assert {n for n, k in embedded.items() if k is Convert} == _CONVERT | _EMBEDDED_CONVERT


def test_menus_and_selections_are_resolve_and_inventory_is_consume():
    assert {n for n, k in _kinds(UnixHostSpec).items() if k is Resolve} == _UNIX_RESOLVE
    assert {n for n, k in _kinds(EmbeddedHostSpec).items() if k is Resolve} == _EMBEDDED_RESOLVE
    assert type(field_kind(UnixHostSpec, "inventory")) is Consume


def test_an_unmarked_field_is_pass():
    assert type(field_kind(HostSpec, "hw_version")) is Pass


def test_an_override_without_a_marker_inherits_its_parents():
    """Inherit HostSpec's Convert when UnixHostSpec re-declares creds with no marker."""
    assert "creds" in UnixHostSpec.__annotations__
    assert field_kind(UnixHostSpec, "creds") is field_kind(HostSpec, "creds")


def test_two_markers_on_one_field_are_refused():
    class Two(UnixHostSpec):
        widget: Annotated[str, Pass(), Consume("x")] = "w"

    with pytest.raises(ValueError, match=r"Two\.widget declares two field kinds"):
        field_kind(Two, "widget")


@pytest.mark.parametrize(
    "marker", [Convert(lambda v, b: v), Resolve(lambda b: 1), Consume("r"), Pass()]
)
def test_a_marker_repr_names_only_the_marker(marker):
    assert repr(marker) == f"otto.host.{type(marker).__name__}"


def test_omit_is_one_object_that_survives_copying():
    assert copy.deepcopy(Omit) is Omit
    assert repr(Omit) == "otto.host.Omit"


def test_markers_stay_out_of_the_json_schema():
    assert "otto.host" not in str(UnixHostSpec.model_json_schema())


_PARITY_ENTRY = {
    "ip": "10.0.0.1",
    "creds": [{"login": "u", "password": "p"}],
    "metadata": {"a": 1},
    "telnet_options": {"port": 99},
    "snmp": {"community": "c"},
    "toolchain": {"sysroot": "/x"},
}


_PARITY_EXPECTED = {
    "creds": [Cred(login="u", password="p")],
    "metadata": {"a": 1},
    "telnet_options": TelnetOptions(port=99),
    "snmp": SnmpOptions(community="c"),
    "toolchain": Toolchain(sysroot=Path("/x")),
}


@pytest.mark.parametrize("name", list(_PARITY_EXPECTED))
def test_each_marker_converts_to_the_runtime_value_the_host_holds(name):
    """The built host holds the runtime value and type each Convert produces, stated here."""
    expected = _PARITY_EXPECTED[name]
    host = create_host_from_dict(dict(_PARITY_ENTRY), element=Element("lab"))
    assert type(getattr(host, name)) is type(expected)
    assert getattr(host, name) == expected
    spec = UnixHostSpec.model_validate(_PARITY_ENTRY)
    assert field_kind(UnixHostSpec, name).fn(getattr(spec, name), None) == expected


def test_command_frame_none_is_omitted_but_snmp_none_is_forwarded():
    assert field_kind(HostSpec, "command_frame").fn(None, None) is Omit
    assert field_kind(HostSpec, "snmp").fn(None, None) is None


def test_runtime_only_fields_are_marked_on_the_class():
    defaulted = [
        "lab_info",
        "inventory_ref",
        "products",
        "dev_tools",
        "shadowed_products",
        "shadowed_dev_tools",
    ]
    for name in defaulted:
        marker = class_marker(UnixHost, name)
        assert isinstance(marker, RuntimeOnly), name
        assert marker.supplied_by is None, name
    assert class_marker(RemoteHost, "element").supplied_by == "element"
    assert class_marker(UnixHost, "ip") is None


def test_class_markers_are_inherited_through_a_redeclaration():
    @dataclasses.dataclass(slots=True, kw_only=True)
    class Redeclares(UnixHost):
        products: list = dataclasses.field(default_factory=list)

    assert isinstance(class_marker(Redeclares, "products"), RuntimeOnly)


def test_profile_inputs_are_the_pass_convert_resolve_fields_but_os_type():
    inputs = profile_inputs(UnixHostSpec)
    assert {"has_bash", "command_frame", "transfer", "valid_transfers"} <= inputs
    assert "os_type" not in inputs
    assert "inventory" not in inputs
    assert "products" not in inputs
