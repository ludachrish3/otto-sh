"""``otto tunnel check``'s probes and the tunnel's socat argv take only an address.

Every socat sink passes its address through ``socat_host``, which refuses
anything that is not an IP address or hostname: shell syntax, socat's own
separators, a bad IPv6 scope. A valid address must still reach socat as one
word across both shells, so the clients are also run for real under ``sh -c``
against a recording ``socat`` stub (:mod:`tests._fixtures.argv_stub`); the
stub records its argv and exits, so the scripts finish at once without a peer.
"""

import pytest

from otto.tunnel.check_probes import bulk_script, echo_argv, handshake_script, trips_script
from otto.tunnel.socat import egress_socat_args, ingress_socat_args, relay_socat_args
from tests._fixtures.argv_stub import SOCAT_REFUSAL, SOCAT_REFUSED, recorded, run_sh, stub_env

VALID = ["router_1.lab.", "fe80::1%eth0"]
"""Accepted addresses that exercise the quoting: ``_``, a trailing dot, a bracketed IPv6."""

_HOST = {"router_1.lab.": "router_1.lab.", "fe80::1%eth0": "[fe80::1%eth0]"}


def _client_addresses(tmp_path) -> list[str]:
    return [call[-1] for call in recorded(tmp_path, "socat")]


@pytest.mark.parametrize("protocol", ["tcp", "udp"])
def test_the_trips_and_bulk_clients_hand_socat_one_address_word(tmp_path, protocol):
    env = stub_env(tmp_path, ["socat"])
    for value in VALID:
        run_sh(trips_script(protocol, value, 61001, 8, trips=1), env)
        run_sh(bulk_script(protocol, value, 61001, 8), env)
    keyword = "TCP4" if protocol == "tcp" else "UDP4"
    want = [f"{keyword}:{_HOST[value]}:61001" for value in VALID]
    got = [address.split(",")[0] for address in _client_addresses(tmp_path)]
    assert sorted(got) == sorted(want * 2)


def test_the_handshake_hands_socat_one_address_word(tmp_path):
    env = stub_env(tmp_path, ["socat"])
    for value in VALID:
        run_sh(handshake_script(value, 61001), env)
    got = sorted(_client_addresses(tmp_path))
    assert got == sorted(f"TCP4:{_HOST[value]}:61001,connect-timeout=5" for value in VALID)


@pytest.mark.parametrize("value", SOCAT_REFUSED)
def test_every_socat_sink_refuses_what_is_not_an_address(value):
    builders = [
        lambda: trips_script("tcp", value, 61001, 8),
        lambda: bulk_script("udp", value, 61001, 8),
        lambda: handshake_script(value, 61001),
        lambda: echo_argv("tcp", value, 61001),
        lambda: echo_argv("udp", value, 61001),
        lambda: ingress_socat_args("tcp", 8080, value, "10.0.0.2", 50000),
        lambda: ingress_socat_args("tcp", 8080, "10.0.0.1", value, 50000),
        lambda: relay_socat_args("udp", 50000, value),
        lambda: egress_socat_args("tcp", 8080, value, 50000),
    ]
    for build in builders:
        with pytest.raises(ValueError, match=SOCAT_REFUSAL):
            build()


def test_an_ipv6_address_is_bracketed_in_the_socat_address():
    assert (
        echo_argv("tcp", "fe80::1%eth0", 61001)[3]
        == "TCP4-LISTEN:61001,bind=[fe80::1%eth0],fork,reuseaddr"
    )
    assert relay_socat_args("tcp", 50000, "2001:db8::1")[-1] == "TCP4:[2001:db8::1]:50000"
