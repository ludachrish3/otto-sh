"""Duplicate host ids fail loud at every registration path."""

import pytest

from otto.config.lab import Lab
from otto.host.unix_host import UnixHost


def _mk(element, ip="10.0.0.1", element_id=None):
    return UnixHost(ip=ip, creds=[], element=element, element_id=element_id)


def test_add_host_rejects_duplicate():
    lab = Lab(name="t")
    lab.add_host(_mk("server"))
    with pytest.raises((KeyError, ValueError), match="server"):
        lab.add_host(_mk("server", ip="10.0.0.2"))


def test_merge_rejects_colliding_id():
    a = Lab(name="a")
    a.add_host(_mk("server"))
    b = Lab(name="b")
    b.add_host(_mk("server", ip="10.0.0.2"))
    with pytest.raises((KeyError, ValueError), match="server"):
        _ = a + b


def test_same_host_in_two_labs_merges_without_error():
    # A host declared in multiple labs is reconstructed as a distinct object per
    # lab but has the same id AND ip -> dedup on merge, NOT a collision.
    a = Lab(name="a")
    a.add_host(_mk("server", ip="10.0.0.5"))
    b = Lab(name="b")
    b.add_host(_mk("server", ip="10.0.0.5"))  # same id, same ip = the same host
    merged = a + b  # must not raise
    assert merged.hosts["server"].ip == "10.0.0.5"


def test_merge_error_names_pre_merge_lab_not_already_merged_name():
    """The duplicate-id error must name the lab as it was BEFORE the merge
    (``self.name`` is reassigned to ``f"{self.name}_{other.name}"`` before
    the guard formats its message) — otherwise the error misleadingly
    blames a lab name ("a_b") that didn't exist when the collision arose.
    """
    a = Lab(name="a")
    a.add_host(_mk("server"))
    b = Lab(name="b")
    b.add_host(_mk("server", ip="10.0.0.2"))
    with pytest.raises((KeyError, ValueError), match=r"in 'a' vs 10\.0\.0\.2 in 'b'"):
        _ = a + b


def test_distinct_slug_collision_detected():
    # Two different raw elements that slug to the same id collide.
    a = Lab(name="a")
    a.add_host(UnixHost(ip="10.0.0.1", creds=[], element="Lab X Server"))
    with pytest.raises((KeyError, ValueError), match="lab-x-server"):
        a.add_host(UnixHost(ip="10.0.0.2", creds=[], element="lab-x-server"))
