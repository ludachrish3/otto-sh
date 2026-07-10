from otto.link.model import (
    Link,
    LinkEndpoint,
    Provenance,
)


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
