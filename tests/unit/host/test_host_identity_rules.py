"""The id/name/duplicate table of spec 2026-09-05 §6, pinned as literals."""

import pytest

from otto.config.lab import Lab
from otto.host.element import Element
from otto.host.remote_host import make_host_id
from otto.host.unix_host import UnixHost
from otto.labs.errors import LabRepositoryError
from otto.labs.json_repository import parse_elements


@pytest.mark.parametrize(
    ("element", "board", "slot", "expected"),
    [
        ("server", None, None, "server"),
        ("Lab X Server", None, None, "lab-x-server"),
        ("chassis", "cpu", 1, "chassis_cpu1"),
        ("chassis", "io", 7, "chassis_io7"),
        ("Edge Router", "LineCard", 3, "edge-router_linecard3"),
        ("server", "cpu", None, "server_cpu"),
    ],
)
def test_make_host_id_table(element, board, slot, expected):
    assert make_host_id(element, board, slot) == expected


def test_make_host_id_has_no_element_id_parameter():
    import inspect

    assert list(inspect.signature(make_host_id).parameters) == ["element", "board", "slot"]


def test_element_id_never_reaches_the_host_id_or_name():
    host = UnixHost(ip="10.0.0.1", creds=[], element=Element("server", id=103))
    assert host.id == "server"
    assert host.name == "server"
    assert host.element.id == 103


def _entry(name, **extra):
    return {"name": name, "labs": ["l"], "hosts": [{"ip": "10.0.0.1"}], **extra}


def test_identical_names_in_one_file_are_a_duplicate_even_with_ids():
    with pytest.raises(
        LabRepositoryError,
        match=r"duplicate element 'server' at elements\[0\] and elements\[1\]",
    ):
        parse_elements([_entry("server", id=47), _entry("server", id=103)], "x/lab.json")


@pytest.mark.parametrize(
    ("first", "second", "slug"),
    [("server", "Server", "server"), ("server 1", "server_1", "server-1")],
)
def test_names_that_slug_alike_are_a_duplicate(first, second, slug):
    with pytest.raises(
        LabRepositoryError,
        match=rf"elements\[0\] {first!r} and elements\[1\] {second!r} both slug to {slug!r}",
    ):
        parse_elements([_entry(first), _entry(second)], "x/lab.json")


def test_distinct_names_are_distinct_elements():
    parsed = parse_elements([_entry("server1"), _entry("server2")], "x/lab.json")
    assert [e.key for e in parsed] == ["server1", "server2"]


def test_two_board_less_hosts_of_one_element_collide_with_the_new_wording():
    lab = Lab(name="t")
    lab.add_host(UnixHost(ip="10.0.0.1", creds=[], element=Element("server")))
    other = Lab(name="u")
    other.add_host(UnixHost(ip="10.0.0.2", creds=[], element=Element("server")))
    with pytest.raises(ValueError, match="Give the elements distinct names, or set board/slot"):
        _ = lab + other
