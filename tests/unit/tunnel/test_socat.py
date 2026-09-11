"""Exact-argv assertions for the socat builders (spec #2b §6.1/§6.3)."""

import pytest

from otto.host.daemon import launch_command
from otto.tunnel.discovery import DISCOVERY_PS_COMMAND
from otto.tunnel.socat import (
    FREE_PORT_PROBE_COMMAND,
    SOCKET_DUMP_COMMAND,
    NoFreePortError,
    carrier_port_floor,
    egress_socat_args,
    ingress_socat_args,
    parse_ephemeral_ceiling,
    parse_listening_ports,
    parse_port_holders,
    pick_free_port,
    relay_socat_args,
)


class TestBuilders:
    def test_ingress_binds_data_ip(self) -> None:
        assert ingress_socat_args("udp", 5000, "10.10.200.11", "10.10.200.12", 50001) == [
            "socat",
            "UDP4-LISTEN:5000,bind=10.10.200.11,fork,reuseaddr",
            "TCP4:10.10.200.12:50001",
        ]

    def test_ingress_tcp(self) -> None:
        args = ingress_socat_args("tcp", 8080, "10.0.0.1", "10.0.0.2", 49900)
        assert args[1].startswith("TCP4-LISTEN:8080,bind=10.0.0.1,")

    def test_relay_same_port_both_sides(self) -> None:
        assert relay_socat_args(50001, "10.10.200.13") == [
            "socat",
            "TCP4-LISTEN:50001,fork,reuseaddr",
            "TCP4:10.10.200.13:50001",
        ]

    def test_egress_delivers_loopback_default_shape(self) -> None:
        assert egress_socat_args("udp", 5000, "127.0.0.1", 50001) == [
            "socat",
            "TCP4-LISTEN:50001,fork,reuseaddr",
            "UDP4:127.0.0.1:5000",
        ]

    def test_egress_dest_override(self) -> None:
        args = egress_socat_args("udp", 5000, "10.10.200.14", 50001)
        assert args[2] == "UDP4:10.10.200.14:5000"


class TestLaunchAndDiscovery:
    def test_launch_command_survival_shape(self) -> None:
        cmd = launch_command("otto-tunnel:v1:x", ["socat", "A", "B"])
        # Live-bed-validated template (spec §6.4, hardened 2026-07-10):
        # systemd-run --user branch (bounded by `timeout 5` so a hang-shaped
        # dbus breakage still folds through) + setsid fallback, exec -a
        # tagging, no hardcoded program name — the whole if/then/else/fi is
        # wrapped in an outer `bash -c` so the string is one opaque word,
        # safe for a caller to sudo-prefix by naive textual composition.
        assert cmd.startswith("bash -c ")
        assert "timeout 5 systemd-run --user --collect --quiet" in cmd
        assert "setsid bash -c" in cmd
        assert "command -v systemd-run" in cmd
        assert '\'exec -a "$1" "${@:2}"\'' in cmd
        assert cmd.count("socat") == 2  # once per branch, never a doubled argv

    def test_discovery_ps_targets_new_prefix(self) -> None:
        assert "' otto-tunnel:'" in DISCOVERY_PS_COMMAND
        assert "etime=" in DISCOVERY_PS_COMMAND
        assert "etimes" not in DISCOVERY_PS_COMMAND.replace("etime=", "")
        assert "otto-link" not in DISCOVERY_PS_COMMAND


class TestPorts:
    def test_parse_and_pick(self) -> None:
        used = parse_listening_ports("LISTEN 0 128 0.0.0.0:49152 ...\nLISTEN 0 5 [::]:49153 ...")
        assert {49152, 49153} <= used
        assert pick_free_port(used) == 49154

    def test_exhaustion_raises(self) -> None:
        # NoFreePortError, still a RuntimeError so `otto tunnel add`'s
        # `except (ValueError, RuntimeError)` renders it unchanged.
        with pytest.raises(NoFreePortError):
            pick_free_port(set(range(49152, 65536)))
        assert issubclass(NoFreePortError, RuntimeError)


class TestEphemeralRange:
    """Carrier ports must sit where the kernel can never auto-assign them (#284).

    The probe reports only LISTENING sockets, so a port held as the *source*
    port of an outbound connection is invisible to it — and ``reuseaddr`` does
    not waive that conflict (it waives ``TIME_WAIT`` and other reuse-flagged
    sockets only). A carrier port drawn from inside the kernel's ephemeral
    range can therefore be stolen between the probe and the bind, and socat
    dies with ``EADDRINUSE``.
    """

    def test_probe_asks_for_the_ephemeral_range_and_the_listeners(self) -> None:
        assert "ip_local_port_range" in FREE_PORT_PROBE_COMMAND
        assert "ss -Htln" in FREE_PORT_PROBE_COMMAND
        assert "netstat -tln" in FREE_PORT_PROBE_COMMAND

    def test_parses_the_ceiling(self) -> None:
        out = "otto-ephemeral 32768\t60999\nLISTEN 0 128 0.0.0.0:49152 0.0.0.0:*\n"
        assert parse_ephemeral_ceiling(out) == 60999

    def test_silent_host_yields_no_ceiling(self) -> None:
        assert parse_ephemeral_ceiling("LISTEN 0 128 0.0.0.0:49152 0.0.0.0:*\n") is None

    def test_range_line_contributes_no_phantom_listeners(self) -> None:
        """The marker line must not read as two bound ports."""
        used = parse_listening_ports("otto-ephemeral 32768\t60999\n")
        assert used == set()

    def test_floor_clears_the_ceiling(self) -> None:
        assert carrier_port_floor([60999]) == 61000

    def test_floor_clears_the_highest_ceiling_on_the_chain(self) -> None:
        """One permissive host in the chain governs: its kernel is the thief."""
        assert carrier_port_floor([60999, 61234, 55000]) == 61235

    def test_floor_never_drops_below_the_legacy_dynamic_floor(self) -> None:
        """A tight ephemeral range must not push carriers into registered ports."""
        assert carrier_port_floor([40000]) == 49152

    def test_silent_chain_keeps_the_legacy_floor(self) -> None:
        assert carrier_port_floor([]) == 49152

    def test_no_room_above_the_ceiling_falls_back(self) -> None:
        """A host whose ephemeral range runs to the top has nowhere safe left.

        Falling back to the legacy window is strictly better than refusing to
        build a tunnel at all — the post-add verify still catches a collision.
        """
        assert carrier_port_floor([65535]) == 49152
        assert carrier_port_floor([65500]) == 49152

    def test_default_linux_range_puts_carriers_out_of_reach(self) -> None:
        """The regression: 49152 is INSIDE the default 32768-60999 range."""
        floor = carrier_port_floor([60999])
        assert floor > 60999
        assert pick_free_port(set(), lo=floor) == 61000
        assert pick_free_port({61000}, lo=floor) == 61001


class TestPortHolders:
    """A post-add verify failure must be able to name what holds the port (#284).

    ``ss -Htln`` (the allocation probe) shows listeners only, so the thief in a
    port race is invisible to it. The diagnosis probe dumps ALL states, which
    is what makes "the port was stolen" distinguishable from "socat died for
    some other reason" without re-running the whole suite.
    """

    def test_dump_asks_for_every_socket_state(self) -> None:
        assert "ss -Htan" in SOCKET_DUMP_COMMAND
        assert "netstat -tan" in SOCKET_DUMP_COMMAND
        assert "-Htln" not in SOCKET_DUMP_COMMAND, "listeners-only would miss the thief"

    def test_finds_an_established_holder_ss(self) -> None:
        dump = "LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*\nESTAB  0 0    10.0.0.1:61000 10.0.0.9:443\n"
        assert parse_port_holders(dump, 61000) == ["ESTAB  0 0    10.0.0.1:61000 10.0.0.9:443"]

    def test_finds_an_established_holder_netstat(self) -> None:
        dump = (
            "tcp 0 0 0.0.0.0:22       0.0.0.0:*     LISTEN\n"
            "tcp 0 0 10.0.0.1:61000   10.0.0.9:443  ESTABLISHED\n"
        )
        holders = parse_port_holders(dump, 61000)
        assert len(holders) == 1
        assert "ESTABLISHED" in holders[0]

    def test_matches_the_local_port_only_never_the_peer(self) -> None:
        """A remote peer on :61000 is somebody else's listener, not our thief."""
        dump = "ESTAB 0 0 10.0.0.1:45000 10.0.0.9:61000\n"
        assert parse_port_holders(dump, 61000) == []

    def test_free_port_has_no_holders(self) -> None:
        assert parse_port_holders("LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*\n", 61000) == []

    def test_ragged_output_is_survivable(self) -> None:
        """The diagnosis runs on a failing host; it must not raise on junk."""
        assert parse_port_holders("", 61000) == []
        assert parse_port_holders("wat\nss: command not found\n", 61000) == []
