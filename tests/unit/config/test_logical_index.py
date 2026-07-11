"""Lab-assembly stamps a per-element-slug logical index (element_id ascending)."""

from otto.config.lab import Lab
from otto.host.unix_host import UnixHost


def _mk(element, element_id=None, ip="10.0.0.1"):
    return UnixHost(ip=ip, creds=[], element=element, element_id=element_id)


def _lab(*hosts):
    lab = Lab(name="t")
    for h in hosts:
        lab.add_host(h)
    lab._assign_logical_indices()
    return lab


def test_unique_element_has_no_logical_index():
    a = _mk("server")
    _lab(a)
    assert a.logical_index is None


def test_repeated_element_numbered_by_element_id_ascending():
    a = _mk("server", element_id=103)
    b = _mk("server", element_id=47)
    c = _mk("server", element_id=288)
    _lab(a, b, c)
    assert (b.logical_index, a.logical_index, c.logical_index) == (1, 2, 3)


def test_grouping_is_by_element_slug():
    # "Server" and "server" share a slug -> same group.
    a = _mk("Server", element_id=1)
    b = _mk("server", element_id=2)
    _lab(a, b)
    assert (a.logical_index, b.logical_index) == (1, 2)


def test_reassigned_after_merge():
    lab_a = Lab(name="a")
    a = _mk("server", element_id=1)
    lab_a.add_host(a)
    lab_a._assign_logical_indices()
    assert a.logical_index is None  # alone in lab_a

    lab_b = Lab(name="b")
    b = _mk("server", element_id=2, ip="10.0.0.2")
    lab_b.add_host(b)
    lab_a + lab_b  # merge for its side effect (mutates a/b in place)
    assert (a.logical_index, b.logical_index) == (1, 2)  # re-derived over the union
