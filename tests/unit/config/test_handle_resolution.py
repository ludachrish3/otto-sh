"""CLI handle resolution: canonical id wins, positional (element-slug + N) falls back."""

from otto.config.lab import Lab
from otto.host.unix_host import UnixHost


def _lab(*specs):
    lab = Lab(name="t")
    for element, eid, ip in specs:
        lab.add_host(UnixHost(ip=ip, creds=[], element=element, element_id=eid))
    lab._assign_logical_indices()
    return lab


def test_exact_canonical_id_wins():
    lab = _lab(("server", 47, "10.0.0.1"), ("server", 103, "10.0.0.2"))
    assert lab.resolve_handle("server47").id == "server47"


def test_positional_fallback_large_element_ids():
    lab = _lab(("server", 47, "10.0.0.1"), ("server", 103, "10.0.0.2"))
    # No canonical "server1"/"server2" -> positional.
    assert lab.resolve_handle("server1").id == "server47"
    assert lab.resolve_handle("server2").id == "server103"


def test_multiword_element_slug_handle():
    lab = _lab(("Lab X Server", None, "10.0.0.1"))
    assert lab.resolve_handle("lab-x-server").id == "lab-x-server"


def test_unknown_handle_returns_none():
    lab = _lab(("server", 1, "10.0.0.1"))
    assert lab.resolve_handle("nope9") is None
    assert lab.resolve_handle("server5") is None  # no 5th server, no canonical


def test_canonical_shadows_positional():
    # ids {2,5}: canonical "server2" (id 2) wins over positional 2 (id 5).
    lab = _lab(("server", 2, "10.0.0.1"), ("server", 5, "10.0.0.2"))
    assert lab.resolve_handle("server2").id == "server2"  # canonical, not the 2nd


def test_shadow_warning_fires_on_mixed_set(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="otto"):
        _lab(("server", 2, "10.0.0.1"), ("server", 5, "10.0.0.2"))
    assert any("shadows" in r.message for r in caplog.records)


def test_no_shadow_warning_for_large_ids(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="otto"):
        _lab(("server", 47, "10.0.0.1"), ("server", 103, "10.0.0.2"))
    assert not any("shadows" in r.message for r in caplog.records)
