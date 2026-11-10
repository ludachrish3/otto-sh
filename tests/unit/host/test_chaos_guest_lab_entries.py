"""The chaos reboot guest's lab entries, as the factory and the loader see them.

``tests/_fixtures/lab_data/tech1/lab.json`` carries the ``ubu2404`` element, the
``chaos`` lab and the declared data-plane link. Their literals are pinned here
against ``CHAOS_GUEST`` (``scripts/render_chaos_guest_seed.py``), the one
constant the provisioner renders from, so the lab data and the guest the
Vagrant provisioner builds cannot drift apart.
"""

import json

import pytest

from otto.host.factory import create_host_from_dict
from otto.host.unix_host import UnixHost
from otto.labs.loader import load_lab
from scripts.render_chaos_guest_seed import CHAOS_GUEST
from tests._fixtures.labdata import (
    dangling_ends,
    element_of,
    entry_of,
    flat_hosts,
    host_data,
    lab_data_dir,
    lab_data_path,
    register_zephyr_inline_frame,
)
from tests.e2e.chaos._bed import (
    CHAOS_GUEST_DATA_NETDEV,
    CHAOS_GUEST_ELEMENT,
    CHAOS_GUEST_HOP,
    CHAOS_GUEST_HOST_ID,
    CHAOS_HOP_DATA_TAP,
    CHAOS_LAB,
    chaos_guest_target,
)


@pytest.fixture(autouse=True)
def _zephyr_inline_frame() -> None:
    """Loading ``embedded`` builds zephyr27_fat, which declares ``zephyr-inline``."""
    register_zephyr_inline_frame()


def _record(element: str) -> dict:
    doc = json.loads(lab_data_path("tech1").read_text())
    el = next(e for e in doc["elements"] if e["name"] == element)
    return el["hosts"][0]


def test_the_bed_constants_mirror_the_guest_constant():
    assert CHAOS_GUEST.element == CHAOS_GUEST_ELEMENT
    assert CHAOS_GUEST.host_id == CHAOS_GUEST_HOST_ID
    assert CHAOS_GUEST.hop == CHAOS_GUEST_HOP
    assert CHAOS_GUEST.lab == CHAOS_LAB
    assert CHAOS_GUEST.data.netdev == CHAOS_GUEST_DATA_NETDEV
    assert CHAOS_GUEST.data.tap == CHAOS_HOP_DATA_TAP


def test_the_guest_entry_builds_an_ssh_unix_host_behind_test4():
    data = next(h for h in flat_hosts("tech1") if h["element"] == CHAOS_GUEST.element)
    host = create_host_from_dict(entry_of(data), lab_name=CHAOS_GUEST.lab, element=element_of(data))
    assert isinstance(host, UnixHost)
    assert host.id == CHAOS_GUEST.host_id == CHAOS_GUEST_HOST_ID
    assert host.term == "ssh"
    assert host.hop == CHAOS_GUEST.hop
    assert host.ip == CHAOS_GUEST.mgmt.guest_ip
    assert [(c.login, c.password.get_secret_value()) for c in host.creds] == [
        (u.login, u.password) for u in CHAOS_GUEST.users
    ]
    assert data["valid_terms"] == ["ssh"]  # Task 4's health routing keys on this


def test_the_guest_is_in_the_chaos_lab_and_no_other():
    labs = {h["element"]: h["labs"] for h in flat_hosts("tech1", with_labs=True)}
    assert labs[CHAOS_GUEST.element] == ["chaos"]
    assert set(labs["test4"]) == {"embedded", "chaos"}


def test_the_chaos_lab_holds_exactly_test4_and_the_guest():
    doc = json.loads(lab_data_path("tech1").read_text())
    assert doc["labs"]["chaos"]["resources"] == ["test4", CHAOS_GUEST.element]


def test_the_declared_link_names_both_ends_the_provisioner_creates():
    doc = json.loads(lab_data_path("tech1").read_text())
    guest = _record(CHAOS_GUEST.element)
    test4 = _record(CHAOS_GUEST.hop)
    assert guest["interfaces"] == {
        nic.netdev: {"ip": nic.guest_ip, "subnet": nic.subnet}
        for nic in (CHAOS_GUEST.mgmt, CHAOS_GUEST.data)
    }
    assert test4["interfaces"][CHAOS_GUEST.data.tap] == {
        "ip": CHAOS_GUEST.data.host_ip,
        "subnet": CHAOS_GUEST.data.subnet,
    }
    matches = [
        link
        for link in doc["links"]
        if {(e["host"], e["interface"]) for e in link["endpoints"]}
        == {
            (CHAOS_GUEST.hop, CHAOS_GUEST.data.tap),
            (CHAOS_GUEST.host_id, CHAOS_GUEST.data.netdev),
        }
    ]
    assert len(matches) == 1
    assert matches[0]["protocol"] == "udp"


def test_the_management_tap_is_declared_nowhere():
    assert CHAOS_GUEST.mgmt.tap not in lab_data_path("tech1").read_text()


def test_the_loaded_chaos_lab_declares_the_link_with_no_dangling_end():
    lab = load_lab("chaos", search_paths=[lab_data_dir() / "tech1"])
    assert "test4--ubu2404-qemu" in {link.id for link in lab.static_links()}
    assert dangling_ends(lab) == []


def test_the_embedded_lab_is_half_outside_by_design():
    """test4 is in `embedded`, the guest is not: the loader keeps a link with one loaded end
    (link/derive.py), so `-l embedded link repair --all` reports this end. The chaos lab
    exists so no sweep is aimed at `embedded`."""
    lab = load_lab("embedded", search_paths=[lab_data_dir() / "tech1"])
    assert dangling_ends(lab) == ["test4--ubu2404-qemu: ubu2404-qemu"]


def test_chaos_guest_target_names_the_host_the_loader_builds():
    """The guest target aims at the id the loader derives from the committed record,
    through the hop's address and first cred (shape of the BusyBox target pin)."""
    target = chaos_guest_target()
    data = next(h for h in flat_hosts("tech1") if h["element"] == CHAOS_GUEST_ELEMENT)
    loaded = create_host_from_dict(entry_of(data), lab_name=CHAOS_LAB, element=element_of(data))
    assert target.host_id == loaded.id == CHAOS_GUEST_HOST_ID
    assert target.lab == CHAOS_LAB
    hop = host_data(CHAOS_GUEST_HOP)
    assert target.ssh_host == hop["ip"]
    assert target.ssh_username == hop["creds"][0]["login"]
    assert target.ssh_password == hop["creds"][0]["password"]
