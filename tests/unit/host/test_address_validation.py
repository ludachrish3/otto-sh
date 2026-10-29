"""One address rule for every field that holds an address (``validate_address``).

A host's ``ip``, an inventory record's ``ip`` and an interface's ``ip`` are
spliced into commands a host runs, so the model refuses anything that is not
an IP address or, where a name is allowed, an RFC 1123 hostname. The value is
refused with the field named, before it can reach a command.
"""

import pytest
from pydantic import ValidationError

from otto.host.address import validate_address
from otto.host.option_specs import SnmpOptionsSpec
from otto.host.specs import HostSpec, InterfaceSpec
from otto.link.netem import NetEmImpairer
from otto.link.params import ImpairmentParams
from otto.models.inventory import InventoryRecord
from tests._fixtures.argv_stub import recorded, run_sh, stub_env

REFUSED = ["1.2.3.4'; touch /tmp/pwned; '", "fe80::1%eth0;id", "a b"]
ACCEPTED = ["router1.lab", "fe80::1%eth0", "10.0.0.5"]
REFUSAL = r"is not a valid IP address|has scope ID"


@pytest.mark.parametrize("model", [HostSpec, InventoryRecord])
@pytest.mark.parametrize("value", REFUSED)
def test_a_management_address_that_is_neither_ip_nor_hostname_is_refused_naming_ip(model, value):
    with pytest.raises(ValidationError, match=REFUSAL) as excinfo:
        model(ip=value)
    assert [err["loc"] for err in excinfo.value.errors()] == [("ip",)]


@pytest.mark.parametrize("model", [HostSpec, InventoryRecord])
@pytest.mark.parametrize("value", ACCEPTED)
def test_a_management_address_may_be_an_ip_or_a_hostname(model, value):
    assert model(ip=value).ip == value


@pytest.mark.parametrize("value", REFUSED)
def test_an_interface_address_is_refused_naming_its_interface(value):
    with pytest.raises(ValidationError, match=REFUSAL) as excinfo:
        HostSpec(ip="10.0.0.1", interfaces={"eth0": value})
    assert [err["loc"] for err in excinfo.value.errors()] == [("interfaces", "eth0", "ip")]


def test_an_interface_address_takes_no_hostname():
    """An interface is a netdev's own address, so a name is refused there."""
    with pytest.raises(ValidationError, match="is not a valid IP address"):
        InterfaceSpec(ip="router1.lab")
    assert InterfaceSpec(ip="fe80::1%eth0").ip == "fe80::1%eth0"
    assert InterfaceSpec(ip="10.0.0.5").ip == "10.0.0.5"


def test_an_inventory_records_interfaces_follow_the_same_rule():
    with pytest.raises(ValidationError, match=REFUSAL) as excinfo:
        InventoryRecord(ip="10.0.0.1", interfaces={"eth2": "fe80::1%eth0;id"})
    assert [err["loc"] for err in excinfo.value.errors()] == [("interfaces", "eth2", "ip")]


@pytest.mark.parametrize(
    "value",
    [
        "10.0.0.256",  # an all-digit last label is a mistyped IPv4 address, not a name
        "-router.lab",
        "router-.lab",
        "a" * 64 + ".lab",  # a label longer than 63
        ".".join(["a" * 63] * 4),  # longer than 253
        "router1.lab..",
        ".",
        "a..b",
        "",
    ],
)
def test_the_rule_refuses_what_is_not_a_hostname(value):
    with pytest.raises(ValueError, match="is not a valid IP address or DNS hostname"):
        validate_address(value, allow_hostname=True)


@pytest.mark.parametrize("value", ["fe80::1%" + "e" * 16, "fe80::1%eth 0", "fe80::1%-x"])
def test_the_rule_refuses_a_scope_id_that_is_not_an_interface_name(value):
    with pytest.raises(ValueError, match="has scope ID"):
        validate_address(value, allow_hostname=True)


@pytest.mark.parametrize(
    "value",
    [
        "fe80::1%" + "e" * 15,
        "fe80::1%3",
        "2001:db8::1",
        "h",
        "a-1.b-2.c3",
        "xn--bcher-kva.ch",
        "router_1.lab",  # lab and Active Directory names use '_'
        "router1.lab.",  # one trailing dot: a fully qualified name
        "_ldap._tcp.corp",
    ],
)
def test_the_rule_accepts_the_edges_of_what_it_allows(value):
    assert validate_address(value, allow_hostname=True) == value


@pytest.mark.parametrize("model", [HostSpec, InventoryRecord])
def test_any_interface_key_loads_and_reaches_tc_as_one_word(model, tmp_path):
    """Interface keys are not validated (NetBox names ``GigabitEthernet0/1``); tc quotes them."""
    marker = tmp_path / "pwned"
    keys = ["Gi0/1", f"eth0;touch {marker}", f"$(touch {marker})"]
    loaded = model(ip="10.0.0.1", interfaces=dict.fromkeys(keys, "10.0.0.5")).interfaces
    assert list(loaded) == keys
    impairer = NetEmImpairer()
    env = stub_env(tmp_path, ["tc"])
    for key in keys:
        for cmd in [
            impairer.apply_command(key, ImpairmentParams(delay_ms=5.0)),
            impairer.read_command(key),
            impairer.clear_command(key),
            impairer.scoped_root_command(key),
            *impairer.scoped_read_commands(key),
        ]:
            run_sh(cmd, env)
    calls = recorded(tmp_path, "tc")
    assert calls
    for call in calls:
        assert call[call.index("dev") + 1] in keys, call
    assert not marker.exists()


def test_an_snmp_address_follows_the_same_rule():
    assert SnmpOptionsSpec(address="router_1.lab").address == "router_1.lab"
    with pytest.raises(ValidationError, match="is not a valid IP address") as excinfo:
        SnmpOptionsSpec(address="1.2.3.4; reboot")
    assert [err["loc"] for err in excinfo.value.errors()] == [("address",)]
