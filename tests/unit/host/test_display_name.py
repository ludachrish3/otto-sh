"""Display name = element name, board, slot — as written, no number (spec 2026-09-05 §2.4)."""

from otto.config.lab import Lab
from otto.host.element import Element
from otto.host.unix_host import UnixHost


def _mk(element, board=None, slot=None, name="", ip="10.0.0.1"):
    return UnixHost(ip=ip, creds=[], element=Element(element), board=board, slot=slot, name=name)


def test_unique_element_original_case():
    assert _mk("Lab X Server").name == "Lab X Server"


def test_board_and_slot_space_separated_original_case():
    assert _mk("Node", board="Blade", slot=3).name == "Node Blade 3"


def test_mixed_case_board_is_kept_verbatim():
    host = _mk("Edge Router", board="LineCard", slot=3)
    assert host.name == "Edge Router LineCard 3"
    assert host.id == "edge-router_linecard3"


def test_multi_host_element_gets_no_number_after_lab_assembly():
    lab = Lab(name="t")
    a = _mk("chassis", board="cpu", slot=1)
    b = _mk("chassis", board="cpu", slot=2, ip="10.0.0.2")
    c = _mk("chassis", board="io", slot=7, ip="10.0.0.3")
    for host in (a, b, c):
        lab.add_host(host)
    merged = lab + Lab(name="u")
    assert [h.name for h in (a, b, c)] == ["chassis cpu 1", "chassis cpu 2", "chassis io 7"]
    assert merged.hosts[a.id] is a


def test_explicit_name_override_wins():
    assert _mk("Server", name="The Big One").name == "The Big One"


def test_host_has_no_logical_index_attribute():
    assert not hasattr(_mk("Server"), "logical_index")


def test_str_is_the_display_name():
    h = _mk("Lab X Server")
    assert str(h) == "Lab X Server"
    assert f"{h}" == "Lab X Server"
    assert h.id == "lab-x-server"
