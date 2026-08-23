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
from otto.host.options import NcOptions, TelnetOptions
from otto.host.unix_host import UnixHost
from scripts.build_busybox_guest_images import GUEST_TABLE
from tests._fixtures.labdata import lab_data_path
from tests.conftest import BUSYBOX_GUEST_NES

BUSYBOX_LINK_ELEMENT = "bb1350"
"""The one guest whose wire to test1 is DECLARED as a link -- the chaos lane's
fixed anchor (``tests/e2e/chaos/_bed.BUSYBOX_CHAOS_ELEMENT``). The other four
TAPs are just as real and are documented in the bed's architecture page;
nothing names them, and a declaration nothing names is one more hand-written
copy of the identity table with no reader to catch it drifting."""

_BED_SUBNET = "198.51.100.16/30"
"""bb1350's /30. Spelled once and asserted on BOTH ends below: two halves of one
wire disagreeing about their subnet is exactly the copy error a declaration can
make and a live bed cannot feel."""


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
        assert host.hop == "test1"
        # The guest's OWN address on its /30, not the hop's and not loopback:
        # each guest sits behind a TAP that exists only on test1, so the
        # address is real but routable from test1 alone. A LAN IP here would
        # send otto at some other lab host with every hostless gate still
        # green; loopback (what the QEMU-hostfwd arrangement needed) would send
        # it at the machine running otto.
        assert host.ip in {g.ip for g in GUEST_TABLE}


def test_guest_entries_address_the_ips_the_bed_actually_assigns():
    """Lab data and the image builder's ``GUEST_TABLE`` are two hand-written
    copies of one identity table — the builder's copy is what each guest's own
    ``rcS`` configures on ``eth0``, and what the Vagrantfile provisioner
    mirrors into the TAP each guest's QEMU attaches to. A digit wrong on this
    side aims otto at an address nothing answers on (or, worse, at the
    neighbouring guest), and every hostless gate stays green: only a live bed
    would show it. Compare the two directly instead."""
    guests = _guest_entries()
    committed = [(g["sw_version"], g["element"], g["ip"]) for g in guests]
    assert committed == [(g.version, g.element, g.ip) for g in GUEST_TABLE]


def test_guest_entries_override_neither_the_telnet_port_nor_the_nc_window():
    """The entries declare no ports, and the DEFAULTS are what that means.

    Both overrides existed only because QEMU user-mode networking had to
    pre-map every port the bed would ever use: telnet arrived on a per-guest
    ``23xx`` hostfwd because 23 was already taken on test1's loopback, and
    ``nc_options.port`` pointed at a ten-port identity-mapped window because
    the nc transfer needs the guest-side and hop-side numbers to match and
    hostfwd could only forward a range chosen in advance. On a real NIC the
    guest owns its whole port space: telnetd binds the honest 23, and the nc
    backend's own free-port scan works the way it does on every other host.

    So this asserts the resolved values, not merely the keys' absence. "No
    override" and "the default I expect" are different claims, and only the
    second one is the reason the entries are allowed to stay quiet — an
    ``NcOptions.port`` default that moved to something test1 already uses
    would break the bed while the entries still looked clean.
    """
    for data in _guest_entries():
        assert "telnet_options" not in data, (
            f"{data['element']} re-declares telnet_options; the guests' telnetd "
            "binds 23 and nothing forwards it any more"
        )
        assert "nc_options" not in data, (
            f"{data['element']} re-declares nc_options; there is no forwarded "
            "port window on a real NIC, so the backend picks its own ports"
        )
        host = create_host_from_dict(data, lab_name="busybox")
        # Both halves spelled with the LITERAL in the chain, not just
        # "resolved == default": `x == Default().x` is true of any default,
        # including one that moved onto a port test1 already uses, which is
        # precisely the drift this test claims to catch.
        assert host.telnet_options.port == 23 == TelnetOptions().port
        assert host.nc_options.port == 9000 == NcOptions().port


def test_the_parity_backend_map_names_exactly_the_bed_roster():
    """The guest SET has three authorities; this is the third edge.

    lab.json is pinned to the image builder's ``GUEST_TABLE`` by the test
    above, and the bed suite derives its ``GUESTS`` from lab.json directly
    (``tests/integration/busybox_bed/conftest.py`` does this same walk). The
    first-party parity rows come from somewhere else entirely: the hand-written
    ``_BUSYBOX_BACKEND_NE`` map in ``tests/conftest.py``, which is what puts the
    guests into the generic ``host1`` / ``transfer_host`` / stability suites.
    Nothing tied that map to the roster.

    So consider adding a sixth guest. The existing roster pins (the element
    list above, and ``test_busybox_bed_guests_hop_through_test1``) do red, and
    an implementer fixes them mechanically -- neither one mentions the parity
    map, so the mechanical fix leaves it behind. The new guest then joins the
    bed suite, which reads lab.json, and silently misses every host1, transfer
    and stability row, which read the map: the "silently half-moved" migration
    §5 forbids by name, arrived at by doing exactly what the failing tests
    asked. This is the assertion that names the map.

    Equality, not containment, so the reverse fails just as loudly: a map entry
    naming a guest the lab no longer provisions parametrizes rows against a
    host that cannot be built.
    """
    roster = {g["element"] for g in _guest_entries()}
    mapped = set(BUSYBOX_GUEST_NES)
    assert mapped == roster, (
        f"the parity map and the bed roster disagree — in lab.json only: "
        f"{sorted(roster - mapped)} (these guests run the bed suite but no host1, "
        f"transfer or stability row); in tests/conftest.py only: "
        f"{sorted(mapped - roster)} (these parametrize rows name a guest the lab "
        f"does not provision)"
    )


def test_the_declared_tap_link_names_both_ends_the_builder_provisions():
    """The declared ``test1:bbeth-1350 <-> bb1350_qemu:eth0`` link is
    pinned to ``GUEST_TABLE``, the same way the guest entries above are.

    A link declaration is a fourth hand-written copy of the identity table --
    the TAP's NAME and the TAP's ADDRESS on test1, plus the guest's own
    address on ``eth0`` -- and it is the copy with the least feedback. A wrong
    digit here breaks no bed test: it produces a link that resolves, lists,
    and is refused with a plausible message naming an interface that carries
    something else. ``tests/e2e/chaos/test_connection_drop.py``'s guest arm
    reads test1's LIVE address table and would catch it, but only on a bed
    run; this catches it hostless.

    Interfaces are compared as a whole MAP, not by lookup, so a stray extra
    entry fails too -- an ``interfaces`` block is where a copied-and-edited
    second TAP would land, and the guest owns exactly one netdev.
    """
    guest = next(g for g in GUEST_TABLE if g.element == BUSYBOX_LINK_ELEMENT)
    data = json.loads(lab_data_path("tech1").read_text())
    hosts = {h["element"]: h for h in data["hosts"]}

    assert hosts[guest.element]["interfaces"] == {
        "eth0": {"ip": guest.ip, "subnet": _BED_SUBNET}
    }, f"{guest.element}'s declared interfaces are not the /30 the builder configures on eth0"
    assert hosts["test1"]["interfaces"].get(guest.tap) == {
        "ip": guest.host_ip,
        "subnet": _BED_SUBNET,
    }, f"test1's declared {guest.tap} is not the hop-side end of {guest.element}'s /30"

    declared = [
        link
        for link in data["links"]
        if {(e["host"], e.get("interface")) for e in link["endpoints"]}
        == {("test1", guest.tap), (f"{guest.element}_qemu", "eth0")}
    ]
    assert len(declared) == 1, (
        f"expected exactly one declared {guest.tap} <-> {guest.element}:eth0 link, "
        f"found {len(declared)}: {declared}"
    )
