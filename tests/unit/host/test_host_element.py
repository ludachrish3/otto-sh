"""``host.element`` is the one path to element data (spec 2026-09-05 §2.5, §4)."""

import pytest

from otto.host.docker_host import DockerContainerHost
from otto.host.element import Element
from otto.host.factory import create_host_from_dict
from otto.host.local_host import LocalHost
from otto.host.unix_host import UnixHost


def test_unix_host_carries_the_element_object():
    element = Element("Lab X Server", id=3, metadata={"k": "v"}, resources=frozenset({"r"}))
    host = UnixHost(ip="10.0.0.1", creds=[], element=element)
    assert host.element is element
    assert host.element.name == "Lab X Server"
    assert host.element.id == 3
    assert host.element.metadata == {"k": "v"}
    assert host.element.resources == frozenset({"r"})


@pytest.mark.parametrize("removed", ["element_id", "element_metadata", "element_resources"])
def test_flat_element_fields_are_gone(removed):
    host = UnixHost(ip="10.0.0.1", creds=[], element=Element("server"))
    assert not hasattr(host, removed)


def test_element_is_required():
    with pytest.raises(TypeError, match="element"):
        UnixHost(ip="10.0.0.1", creds=[])  # type: ignore[call-arg]


def test_the_factory_carries_the_element_through_untouched():
    """One argument in, the same object out — the factory builds no ``Element``.

    Sharing is the point (spec 2026-09-05 §3): two hosts of one element get one
    instance, so an identity check, not an equality one.
    """
    element = Element("dut", id=5, metadata={"rack": 2}, resources=frozenset({"dut-5"}))
    entry = {"ip": "10.0.0.1", "creds": [{"login": "vagrant", "password": "vagrant"}]}
    first = create_host_from_dict(dict(entry), element=element)
    second = create_host_from_dict({**entry, "board": "b"}, element=element)
    assert first.element is element
    assert second.element is first.element
    assert (element.id, element.metadata, element.resources) == (
        5,
        {"rack": 2},
        frozenset({"dut-5"}),
    )


def test_containers_and_local_belong_to_no_element():
    assert LocalHost().element is None
    assert DockerContainerHost.__dataclass_fields__["element"].default is None


def test_display_name_and_id_still_read_the_element_name():
    host = UnixHost(ip="10.0.0.1", creds=[], element=Element("Lab X Server"), board="Blade", slot=2)
    assert host.name == "Lab X Server Blade 2"
    assert host.id == "lab-x-server_blade2"
