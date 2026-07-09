from otto.link.model import (
    Link,
    LinkEndpoint,
    Provenance,
    make_dynamic_link_id,
    make_link_id,
)


def test_dynamic_id_appends_port_suffix():
    a = LinkEndpoint(host="test1", interface="eth0", port=161)
    b = LinkEndpoint(host="test2", interface="eth0", port=161)
    route = make_link_id(a, b, "udp")
    assert make_dynamic_link_id(a, b, "udp", 161) == f"{route}-161"


def test_dynamic_link_computes_suffixed_id():
    a = LinkEndpoint(host="test1", interface="eth0", port=161)
    b = LinkEndpoint(host="test2", interface="eth0", port=161)
    link = Link(a=a, b=b, protocol="udp", provenance=Provenance.DYNAMIC)
    assert link.id == f"{make_link_id(a, b, 'udp')}-161"


def test_dynamic_id_falls_back_to_zero_when_both_ports_unbound():
    """A DYNAMIC link with no port on either end suffixes ``-0`` (the ``port or
    0`` fallback in ``__post_init__``), so the id is still well-formed."""
    a = LinkEndpoint(host="test1")
    b = LinkEndpoint(host="test2")
    link = Link(a=a, b=b, protocol="udp", provenance=Provenance.DYNAMIC)
    assert link.id == f"{make_link_id(a, b, 'udp')}-0"


def test_static_declared_id_uses_name_when_present():
    a = LinkEndpoint(host="test1")
    b = LinkEndpoint(host="test2")
    link = Link(a=a, b=b, protocol="tcp", provenance=Provenance.DECLARED, name="mgmt")
    assert link.id == "mgmt"


def test_static_id_falls_back_to_sorted_endpoints():
    a = LinkEndpoint(host="test2")
    b = LinkEndpoint(host="test1")
    link = Link(a=a, b=b, protocol="ssh", provenance=Provenance.IMPLICIT)
    assert link.id == "test1--test2"  # sorted, so a<->b == b<->a


def test_explicit_id_is_preserved():
    a = LinkEndpoint(host="test1", port=161)
    b = LinkEndpoint(host="test2", port=161)
    link = Link(a=a, b=b, protocol="udp", provenance=Provenance.DYNAMIC, id="lnk-x-161")
    assert link.id == "lnk-x-161"
