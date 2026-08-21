"""The BusyBox bed guest entries, as the factory sees them.

The five ``bb*`` entries in ``tests/_fixtures/lab_data/tech1/lab.json`` are the
only description of the bed otto has: Phase B's bed conftest selects them by
``"busybox" in labs and hop present`` and reads ``sw_version`` to parametrize
over the pinned versions. Nothing else validates them — a lab entry is data,
and data with a typo in a key name is rejected by the spec (``extra='forbid'``)
only when something builds a host out of it. This module is that something.
"""

import json

from otto.host.factory import create_host_from_dict
from otto.host.unix_host import UnixHost
from scripts.build_busybox_guest_images import GUEST_TABLE
from tests._fixtures.labdata import lab_data_path


def _guest_entries():
    """The bed guests as committed, in lab-data order."""
    hosts = json.loads(lab_data_path("tech1").read_text())["hosts"]
    return [h for h in hosts if "busybox" in h.get("labs", []) and h.get("hop")]


def test_every_busybox_guest_entry_builds_a_telnet_shell_unix_host():
    """The factory must accept the guest entries as committed: unix family,
    term resolved to telnet, transfer to shell, hop preserved. Fails loudly if
    UnixHostSpec rejects hop/sw_version/nc_options."""
    guests = _guest_entries()
    assert [g["element"] for g in guests] == [
        "bb1161",
        "bb1211",
        "bb1281",
        "bb1310",
        "bb1350",
    ]
    for data in guests:
        host = create_host_from_dict(data, lab_name="busybox")
        assert isinstance(host, UnixHost)
        assert host.term == "telnet"
        assert host.transfer == "shell"
        assert host.hop == "carrot_seed"
        # Loopback, not the hop's LAN address: QEMU's hostfwds bind
        # 127.0.0.1 ON carrot, so the guests exist only at the far end of
        # the hop's own loopback. A LAN IP here would send otto at carrot
        # itself (or at nothing) with every hostless gate still green.
        assert host.ip == "127.0.0.1"
        assert host.nc_options.port == data["nc_options"]["port"]


def test_guest_entries_address_the_ports_the_bed_actually_forwards():
    """Lab data and the image builder's ``GUEST_TABLE`` are two hand-written
    copies of one identity table — the builder's copy is what the Vagrantfile
    provisioner mirrors into each guest's hostname and QEMU hostfwds. A digit
    wrong on this side connects otto to a port nothing listens on (or, worse,
    to the neighbouring guest), and every Phase A gate stays green: only a live
    bed would show it. Compare the two directly instead."""
    guests = _guest_entries()
    committed = [
        (g["sw_version"], g["element"], g["telnet_options"]["port"], g["nc_options"]["port"])
        for g in guests
    ]
    assert committed == [(g.version, g.element, g.telnet_port, g.nc_base) for g in GUEST_TABLE]
