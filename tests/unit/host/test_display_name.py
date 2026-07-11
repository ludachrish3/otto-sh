"""Display name = space-joined, original-case element [logical] [board] [slot]."""

from otto.config.lab import Lab
from otto.host.unix_host import UnixHost


def _mk(element, element_id=None, board=None, slot=None, name="", ip="10.0.0.1"):
    return UnixHost(
        ip=ip, creds=[], element=element, element_id=element_id, board=board, slot=slot, name=name
    )


def test_unique_element_no_number_original_case():
    # Standalone host (no lab assembly) -> logical_index is None -> no number.
    h = _mk("Lab X Server")
    assert h.name == "Lab X Server"


def test_board_and_slot_space_separated_original_case():
    h = _mk("Node", board="Blade", slot=3)
    assert h.name == "Node Blade 3"


def test_repeated_element_shows_logical_index():
    a = _mk("Server", element_id=103)
    b = _mk("Server", element_id=47, ip="10.0.0.2")
    lab = Lab(name="t")
    lab.add_host(a)
    lab.add_host(b)
    lab._assign_logical_indices()
    assert b.name == "Server 1"
    assert a.name == "Server 2"


def test_explicit_name_override_wins():
    h = _mk("Server", element_id=1, name="The Big One")
    lab = Lab(name="t")
    lab.add_host(h)
    lab.add_host(_mk("Server", element_id=2, ip="10.0.0.9"))
    lab._assign_logical_indices()
    assert h.name == "The Big One"  # override survives assembly


def test_board_host_with_logical_index_orders_parts():
    a = _mk("Node", element_id=1, board="Blade", slot=2)
    b = _mk("Node", element_id=2, board="Blade", slot=5, ip="10.0.0.2")
    lab = Lab(name="t")
    lab.add_host(a)
    lab.add_host(b)
    lab._assign_logical_indices()
    assert a.name == "Node 1 Blade 2"
    assert b.name == "Node 2 Blade 5"


def test_str_is_the_display_name():
    # print(host) / f"{host}" / "%s" logging render the friendly display name,
    # not the correlation id or the dataclass repr. id stays the slug.
    h = _mk("Lab X Server")
    assert str(h) == "Lab X Server"
    assert f"{h}" == "Lab X Server"
    assert h.id == "lab-x-server"  # correlation id is unchanged


def test_str_reflects_logical_index_when_element_repeats():
    a = _mk("Server", element_id=1)
    b = _mk("Server", element_id=2, ip="10.0.0.2")
    lab = Lab(name="t")
    lab.add_host(a)
    lab.add_host(b)
    lab._assign_logical_indices()
    assert str(a) == "Server 1"
    assert str(b) == "Server 2"
