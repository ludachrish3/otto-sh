from otto.link.socat import (
    DISCOVERY_PS_COMMAND,
    egress_socat_args,
    ingress_socat_args,
    launch_command,
    parse_listening_ports,
    pick_free_port,
)


def test_ingress_udp_bridges_udp_listen_to_tcp_carrier():
    assert ingress_socat_args("udp", 161, "10.0.0.2", 50001) == [
        "socat",
        "UDP4-LISTEN:161,fork,reuseaddr",
        "TCP4:10.0.0.2:50001",
    ]


def test_egress_udp_bridges_tcp_carrier_to_udp_dest():
    assert egress_socat_args("udp", 161, "10.0.0.9", 50001) == [
        "socat",
        "TCP4-LISTEN:50001,fork,reuseaddr",
        "UDP4:10.0.0.9:161",
    ]


def test_tcp_tunnel_uses_tcp_on_both_legs():
    assert ingress_socat_args("tcp", 8080, "10.0.0.2", 50002) == [
        "socat",
        "TCP4-LISTEN:8080,fork,reuseaddr",
        "TCP4:10.0.0.2:50002",
    ]
    assert egress_socat_args("tcp", 8080, "10.0.0.9", 50002) == [
        "socat",
        "TCP4-LISTEN:50002,fork,reuseaddr",
        "TCP4:10.0.0.9:8080",
    ]


def test_launch_command_tags_argv0_and_survives_session():
    sentinel = "otto-link:v1:lnk-x-161:udp:test1::161:test2::161"
    cmd = launch_command(
        sentinel,
        ["socat", "UDP4-LISTEN:161,fork,reuseaddr", "TCP4:10.0.0.2:50001"],
    )
    assert sentinel in cmd
    # argv[0]-tagging template — does NOT hardcode the program name (socat_args[0]
    # is already "socat"). A doubled `socat` (regression) would run
    # `socat socat <addr> <addr>` and die on the bogus third address.
    assert '\'exec -a "$1" "${@:2}"\'' in cmd
    assert "socat socat" not in cmd
    # the full tagged argv appears: _ <sentinel> socat <addr1> <addr2>
    assert f" _ {sentinel} socat UDP4-LISTEN:161,fork,reuseaddr TCP4:10.0.0.2:50001" in cmd
    # must OUTLIVE the ssh session: systemd-run --user (no sudo) on systemd hosts,
    # setsid-detached fallback on non-systemd hosts.
    assert "command -v systemd-run" in cmd
    assert "systemd-run --user --collect --quiet -- bash -c" in cmd
    assert "setsid bash -c" in cmd
    assert "</dev/null >/dev/null 2>&1 &" in cmd


def test_discovery_command_is_portable():
    # portable etime (not etimes), no pgrep -a, and tolerant of no matches
    assert "ps -eo pid=,etime=,args=" in DISCOVERY_PS_COMMAND
    assert "etimes" not in DISCOVERY_PS_COMMAND
    assert "pgrep" not in DISCOVERY_PS_COMMAND
    assert DISCOVERY_PS_COMMAND.rstrip().endswith("|| true")


def test_parse_listening_ports_extracts_from_ss_and_netstat():
    ss = "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\nLISTEN 0 128 127.0.0.1:6010 0.0.0.0:*"
    assert parse_listening_ports(ss) == {22, 6010}


def test_pick_free_port_skips_used():
    assert pick_free_port({49152, 49153}, lo=49152, hi=49155) == 49154


def test_pick_free_port_raises_when_exhausted():
    import pytest

    with pytest.raises(RuntimeError):
        pick_free_port({49152, 49153, 49154}, lo=49152, hi=49154)
