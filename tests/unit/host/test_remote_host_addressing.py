from otto.host.command_frame import ZephyrFrame
from otto.host.embedded_host import EmbeddedHost
from otto.host.unix_host import UnixHost


def _unix(**kw):
    return UnixHost(ip="10.0.0.1", creds={"u": "p"}, element="e", **kw)


def test_unix_interfaces_default_empty():
    assert _unix().interfaces == {}


def test_address_for_returns_literal_unchanged():
    h = _unix()
    assert h.address_for("10.0.0.1") == "10.0.0.1"
    assert h.address_for("203.0.113.9") == "203.0.113.9"


def test_address_for_resolves_named_interface():
    h = _unix(interfaces={"mgmt": "10.9.9.9", "data": "192.168.5.5"})
    assert h.address_for("mgmt") == "10.9.9.9"
    assert h.address_for("data") == "192.168.5.5"
    assert h.address_for("10.0.0.1") == "10.0.0.1"  # literal still passes through


def test_embedded_interfaces_field_and_address_for():
    h = EmbeddedHost(ip="192.0.2.1", element="dut", command_frame=ZephyrFrame())
    assert h.interfaces == {}
    assert h.address_for("192.0.2.1") == "192.0.2.1"
