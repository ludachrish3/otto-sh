"""The entry a command reaches a conformance cell's host by, and the route it must take.

No host is opened here: every cell is picked from ``bed_space()`` or
``hermetic_space()`` by its fields, and the conftest hook is driven through a
recording ``Metafunc`` the way ``tests/unit/test_conformance_bed.py`` drives it.
"""

import ast
import dataclasses
from types import SimpleNamespace

import pytest

from otto.host.factory import create_host_from_dict
from otto.result import CommandResult, Status
from tests._fixtures.labdata import element_for, host_data
from tests.conformance import _entry
from tests.conformance import conftest as _conformance_conftest
from tests.conformance._bed import BED_TECH, bed_space
from tests.conformance._cells import BUSYBOX_ARTIFACT, LOCAL, hermetic_space
from tests.conformance._entry import (
    EMBEDDED,
    EXEC,
    EXEC_SHELL,
    LOCAL_FAMILY,
    RUN,
    UNIX,
    check_route,
    entries_declared,
    entries_of,
    entries_of_node,
    expected_route,
    project,
    proxied_on,
    space_for,
    with_entry,
)
from tests.conformance._resolved import ResolvedCell
from tests.conformance._sample import _rank, cell_label, draw


def lab_entry(resolved: ResolvedCell) -> dict:
    return host_data(resolved.cell.element, BED_TECH)


def test_projection_keeps_one_cell_per_element_and_term():
    projected = project(bed_space())
    pairs = [(r.cell.element, r.cell.term) for r in projected]
    assert len(set(pairs)) == len(pairs)
    assert set(pairs) == {(r.cell.element, r.cell.term) for r in bed_space()}


def test_projection_keeps_the_space_order():
    space = bed_space()
    projected = project(space)
    assert projected == [r for r in space if r in projected]


def test_a_busybox_artifact_cell_has_no_exec_entry():
    local = next(r for r in hermetic_space() if r.kind == LOCAL)
    busybox = next(
        (r for r in hermetic_space() if r.kind == BUSYBOX_ARTIFACT),
        dataclasses.replace(local, kind=BUSYBOX_ARTIFACT),
    )
    assert entries_of(busybox) == [RUN]
    assert entries_of(local) == [RUN, EXEC]
    assert expected_route(with_entry(local, EXEC)) == "local-subprocess"


def test_a_network_hop_does_not_change_the_expected_route():
    hopped = next(r for r in bed_space() if r.cell.term == "ssh" and lab_entry(r).get("hop"))
    assert expected_route(with_entry(hopped, EXEC)) == "ssh-channel"
    assert EXEC_SHELL in entries_of(hopped)
    assert expected_route(with_entry(hopped, EXEC_SHELL)) == "pooled-shell"
    assert hopped.open_shell_host is not None


def test_a_proxied_login_expects_the_pooled_shell():
    proxied = dataclasses.replace(
        next(r for r in bed_space() if r.cell.term == "ssh"), proxied_login=True
    )
    assert expected_route(with_entry(proxied, EXEC)) == "pooled-shell"
    assert EXEC_SHELL not in entries_of(proxied)


def test_no_bed_cell_is_a_proxied_login_today():
    """So the row above is unit-only, and says so."""
    assert not any(r.proxied_login for r in bed_space())


def test_a_proxied_cred_is_read_as_a_proxied_login():
    """The positive control for the fact the venues read: a cred with a ``proxy``.

    Read off the lab entry, and checked against what otto itself resolves from
    the same entry, so the two can only agree by both being right.
    """
    entry = dict(host_data("test1", BED_TECH))
    entry["creds"] = [
        {"login": "root", "proxy": "su", "via": "vagrant"},
        {"login": "vagrant", "password": "vagrant"},
    ]
    direct = dict(host_data("test1", BED_TECH))
    assert proxied_on(entry, "ssh", family=UNIX) is True
    assert proxied_on(direct, "ssh", family=UNIX) is False
    host = create_host_from_dict(entry, element=element_for("test1", BED_TECH))
    assert host._connections.term == "ssh"
    assert host._connections.proxy_hops
    # A cred scoped to another protocol is not the ssh login's pick.
    scoped = dict(direct, creds=[{**entry["creds"][0], "protocols": ["telnet"]}, *direct["creds"]])
    assert proxied_on(scoped, "ssh", family=UNIX) is False
    assert proxied_on(scoped, "telnet", family=UNIX) is True


def test_proxied_on_fails_loudly_when_the_fact_is_missing():
    """A unix entry with no creds is an unreadable fact, not a direct login."""
    with pytest.raises(LookupError, match="declares no creds"):
        proxied_on({"name": "x"}, "ssh", family=UNIX)
    with pytest.raises(ValueError, match="unknown family"):
        proxied_on({"creds": []}, "ssh", family="mystery")
    # Families that cannot proxy answer without reading the entry.
    assert proxied_on({}, "console", family=EMBEDDED) is False
    assert proxied_on({}, "local", family=LOCAL_FAMILY) is False


def test_an_embedded_telnet_cell_expects_the_default_session():
    embedded = next(r for r in bed_space() if r.family == "embedded" and r.cell.term == "telnet")
    assert entries_of(embedded) == [RUN, EXEC]
    assert expected_route(with_entry(embedded, EXEC)) == "default-session"


@pytest.mark.parametrize(
    ("term", "route"), [("telnet", "pooled-shell"), ("console", "default-session")]
)
def test_a_unix_non_ssh_cell_has_no_exec_shell_entry(term, route):
    resolved = next(r for r in bed_space() if r.family == "unix" and r.cell.term == term)
    assert entries_of(resolved) == [RUN, EXEC]
    assert expected_route(with_entry(resolved, EXEC)) == route
    assert resolved.open_shell_host is None


def test_run_is_always_the_default_session():
    for resolved in bed_space() + hermetic_space():
        assert expected_route(with_entry(resolved, RUN)) == "default-session"


def test_the_label_names_the_entry():
    resolved = bed_space()[0]
    assert cell_label(with_entry(resolved, EXEC)).endswith(":exec]")
    assert cell_label(resolved).count(":") == 2


def test_two_entries_of_one_cell_rank_independently():
    resolved = next(r for r in bed_space() if EXEC_SHELL in entries_of(r))
    legs = [with_entry(resolved, entry) for entry in (RUN, EXEC, EXEC_SHELL)]
    orders = {
        tuple(sorted(range(3), key=lambda i, seed=seed: _rank(seed, legs[i])))
        for seed in range(200)
    }
    assert len(orders) == 6, orders
    # And a draw of one keeps a single leg, not a cell's every entry.
    assert {len({r.entry for r in draw(legs, 1, seed)}) for seed in range(20)} == {1}


def test_check_route_fails_on_a_wrong_route():
    an_ssh_cell = next(r for r in bed_space() if r.cell.term == "ssh")
    with pytest.raises(AssertionError, match="ssh-channel"):
        check_route(
            with_entry(an_ssh_cell, EXEC),
            CommandResult(status=Status.Success, route="pooled-shell"),
        )
    check_route(
        with_entry(an_ssh_cell, EXEC), CommandResult(status=Status.Success, route="ssh-channel")
    )


def test_the_entries_marker_is_read_by_ast():
    module = ast.parse(
        "import pytest\n"
        "@pytest.mark.entries('run', 'exec', 'exec-shell')\n"
        "def test_marked(): ...\n"
        "def test_unmarked(): ...\n"
        "@pytest.mark.entries('run', 'teleport')\n"
        "def test_misspelt(): ...\n"
    )
    marked, unmarked, misspelt = module.body[1:]
    assert entries_of_node(marked) == [RUN, EXEC, EXEC_SHELL]
    assert entries_of_node(unmarked) == []
    with pytest.raises(ValueError, match="teleport"):
        entries_of_node(misspelt)


def test_the_command_contracts_declare_their_entries():
    assert entries_declared(
        "tests/conformance/test_exec_contract.py::test_exec_reports_the_documented_exit_code"
    ) == [RUN, EXEC, EXEC_SHELL]
    assert entries_declared(
        "tests/conformance/test_exec_contract.py::test_a_failing_command_is_not_reported_as_success"
    ) == [RUN]
    assert (
        entries_declared(
            "tests/conformance/test_transfer_contract.py::test_put_get_roundtrip_preserves_content"
        )
        == []
    )
    assert entries_declared("tests/conformance/test_fabricated.py::test_contract") == []


def test_space_for_crosses_the_projection_with_the_declared_entries(monkeypatch):
    monkeypatch.setattr(_entry, "entries_declared", lambda contract: [RUN, EXEC_SHELL])
    space = bed_space()
    expected = [
        with_entry(r, e) for r in project(space) for e in (RUN, EXEC_SHELL) if e in entries_of(r)
    ]
    assert space_for("any::test", space) == expected


class _RecordingMetafunc:
    """Enough ``Metafunc`` for the conftest hook; ``config`` answers no seed."""

    def __init__(self, nodeid: str = "tests/conformance/test_fabricated.py::test_contract"):
        self.fixturenames = ["resolved_cell"]
        self.config = SimpleNamespace(
            stash=pytest.Stash(), getoption=lambda name, default=None: default
        )
        self.definition = SimpleNamespace(nodeid=nodeid)
        self.module = SimpleNamespace(__name__="fabricated_contract")
        self.parametrized = None
        self.ids = None

    def parametrize(self, argname, argvalues, ids=None, indirect=False):
        self.parametrized = [param.values[0] for param in argvalues]
        self.ids = [ids(cell) for cell in self.parametrized]


def test_an_unmarked_contract_draws_full_cells(monkeypatch):
    space = bed_space()
    monkeypatch.setattr(_conformance_conftest, "_SPACE", space)
    metafunc = _RecordingMetafunc()
    _conformance_conftest.pytest_generate_tests(metafunc)
    assert metafunc.parametrized == space
    assert all(ident.count(":") == 2 for ident in metafunc.ids)


def test_a_marked_contract_draws_projected_cells_times_its_entries(monkeypatch):
    space = bed_space()
    monkeypatch.setattr(_conformance_conftest, "_SPACE", space)
    monkeypatch.setattr(_entry, "entries_declared", lambda contract: [RUN, EXEC, EXEC_SHELL])
    metafunc = _RecordingMetafunc()
    _conformance_conftest.pytest_generate_tests(metafunc)
    assert metafunc.parametrized == [
        with_entry(r, e)
        for r in project(space)
        for e in (RUN, EXEC, EXEC_SHELL)
        if e in entries_of(r)
    ]
    assert len(set(metafunc.ids)) == len(metafunc.ids)
    # The full draw is memoised apart: an unmarked contract in the same session
    # still gets the entry-less cells.
    monkeypatch.setattr(_entry, "entries_declared", lambda contract: [])
    unmarked = _RecordingMetafunc()
    unmarked.config = metafunc.config
    _conformance_conftest.pytest_generate_tests(unmarked)
    assert unmarked.parametrized == space
