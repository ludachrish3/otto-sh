"""logical_indices() is the single source for positions; Lab stamps must agree."""

from otto.configmodule.lab import Lab, logical_indices
from otto.host.unix_host import UnixHost


def _h(element, element_id=None, ip="10.0.0.1"):
    return UnixHost(ip=ip, element=element, element_id=element_id, creds=[])


def test_unique_element_absent_from_map():
    assert logical_indices([_h("server")]) == {}


def test_repeated_numbered_by_element_id_ascending():
    a = _h("server", 103, "10.0.0.1")
    b = _h("server", 47, "10.0.0.2")
    c = _h("server", 288, "10.0.0.3")
    assert logical_indices([a, b, c]) == {"server47": 1, "server103": 2, "server288": 3}


def test_slug_grouping_case_insensitive():
    a = _h("Server", 1, "10.0.0.1")
    b = _h("server", 2, "10.0.0.2")
    assert logical_indices([a, b]) == {"server1": 1, "server2": 2}


def test_stamps_agree_with_helper():
    # The Lab assembly pass must stamp exactly what the helper computes.
    lab = Lab(name="t")
    lab.add_host(_h("server", 47, "10.0.0.1"))
    lab.add_host(_h("server", 103, "10.0.0.2"))
    lab.add_host(_h("router", ip="10.0.0.3"))  # unique -> None
    lab._assign_logical_indices()
    stamped = {h.id: h.logical_index for h in lab.hosts.values() if h.logical_index is not None}
    assert stamped == logical_indices(lab.hosts.values())
