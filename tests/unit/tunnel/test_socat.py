"""Exact-argv assertions for the socat builders (spec #2b §6.1/§6.3)."""

import pytest

from otto.host.daemon import launch_command
from otto.tunnel.discovery import DISCOVERY_PS_COMMAND
from otto.tunnel.socat import (
    egress_socat_args,
    ingress_socat_args,
    parse_listening_ports,
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
        with pytest.raises(RuntimeError):
            pick_free_port(set(range(49152, 65536)))
