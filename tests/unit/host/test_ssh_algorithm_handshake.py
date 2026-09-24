"""The SSH algorithm matrix, exercised: an in-process handshake per row.

The registry pin in tests/unit/host/test_legacy_ssh_algorithms.py proves a
NAME is registered; it cannot prove asyncssh can still complete a handshake
with it. asyncssh 2.24 registers every ``diffie-hellman-group*`` key
exchange unconditionally (``kex_dh.py`` has no availability flag —
``crypto/dh.py`` imports ``cryptography...asymmetric.dh`` at module import
and only touches it inside ``DH.__init__``), so when cryptography removes
finite-field DH while keeping the module importable, the name pin stays
green and the first red would be the next legacy-dropbear bed run. This
module is what turns that red instead, on the dependency bump, hostless.

An asyncssh server on 127.0.0.1 port 0 and an asyncssh client in the same
event loop negotiate ONE algorithm per family per case — ``kex_algs`` /
``server_host_key_algs`` / ``encryption_algs`` / ``mac_algs`` /
``compression_algs`` each pinned to a single entry, so a successful
handshake IS the proof that entry ran (asyncssh records no kex or host-key
choice in ``get_extra_info``; a successful connect with one candidate on
each side is the only signal available). Cipher, MAC and compression are
also checked directly through ``get_extra_info`` on both connections.

Cases come from ``exercisable()`` (tests/_fixtures/ssh_algorithm_matrix.py),
never written by hand: round-robin over the five families, plus one
extra case per MAC that would otherwise only ever meet an AEAD cipher
(asyncssh ignores ``mac_algs`` once the cipher is AEAD — no HMAC is
computed — so pairing it only with AEAD ciphers would never run its MAC
code path).

pytest wraps every item's setup/call/teardown in its own ``catch_warnings()``
and re-applies the ini ``filterwarnings`` from scratch for that item, so a
module-scoped fixture's filter would be installed once and then discarded
when the first item finishes — dead for the other 33 cases. The fixture
here is function-scoped for exactly that reason, so
``install_asyncssh_warning_filters()`` runs inside every item's warnings
context. Each handshake test also carries
``@pytest.mark.filterwarnings("error")``, which pytest applies AFTER the
ini filters — overriding the ini's own FFDH ignore — so the runtime filter
this module installs is the only thing standing between an FFDH warning and
a failure. Under the locked cryptography 49 there is nothing to silence;
under 50 that combination is what keeps ``-W error`` green here, and THAT
is the proof the filter matches the real warning — the injection test in
test_legacy_ssh_algorithms.py cannot give that proof because the locked
cryptography cannot raise the warning.
"""

import asyncio
from dataclasses import dataclass

import asyncssh
import pytest

from otto.host.connections import install_asyncssh_warning_filters
from tests._fixtures.ssh_algorithm_matrix import (
    CIPHER,
    COMPRESSION,
    HOST_KEY,
    KEX,
    MAC,
    exercisable,
)

# asyncssh ignores mac_algs once the cipher is AEAD (RFC 5647/openssh AEAD
# ciphers carry their own integrity tag), so send_mac reports the cipher's
# own name rather than any negotiated MAC.
AEAD_CIPHERS = frozenset(
    {"chacha20-poly1305@openssh.com", "aes128-gcm@openssh.com", "aes256-gcm@openssh.com"}
)

# Used only for the synthetic extra cases below: a kex/host-key pair that
# always succeeds, paired with a non-AEAD cipher, so an AEAD-only-paired MAC
# still runs its HMAC code path once.
_FILLER_KEX = "diffie-hellman-group14-sha1"
_FILLER_HOST_KEY = "ssh-rsa"
_FILLER_CIPHER = "aes128-ctr"

# One host key per key TYPE the exercisable host-key rows need: rsa-sha2-*,
# ssh-rsa-sha*@ssh.com and ssh-rsa all serve off the one RSA key; every other
# row needs its own type.
_HOST_KEYS = [
    asyncssh.generate_private_key("ssh-rsa"),
    asyncssh.generate_private_key("ssh-dss"),
    asyncssh.generate_private_key("ssh-ed25519"),
    asyncssh.generate_private_key("ssh-ed448"),
    asyncssh.generate_private_key("ecdsa-sha2-nistp256"),
    asyncssh.generate_private_key("ecdsa-sha2-nistp384"),
    asyncssh.generate_private_key("ecdsa-sha2-nistp521"),
    asyncssh.generate_private_key("ecdsa-sha2-1.3.132.0.10"),
]


@dataclass(frozen=True)
class _Case:
    kex: str
    host_key: str
    cipher: str
    mac: str
    compression: str


def _build_cases() -> list[_Case]:
    columns = {
        KEX: exercisable(KEX),
        HOST_KEY: exercisable(HOST_KEY),
        CIPHER: exercisable(CIPHER),
        MAC: exercisable(MAC),
        COMPRESSION: exercisable(COMPRESSION),
    }
    longest = max(len(names) for names in columns.values())
    cases = [
        _Case(
            columns[KEX][i % len(columns[KEX])],
            columns[HOST_KEY][i % len(columns[HOST_KEY])],
            columns[CIPHER][i % len(columns[CIPHER])],
            columns[MAC][i % len(columns[MAC])],
            columns[COMPRESSION][i % len(columns[COMPRESSION])],
        )
        for i in range(longest)
    ]
    # A MAC round-robined onto only AEAD ciphers never ran its HMAC: give it
    # one more case against a cipher that actually uses mac_algs.
    for mac in columns[MAC]:
        pairings = {c.cipher for c in cases if c.mac == mac}
        if pairings and pairings <= AEAD_CIPHERS:
            cases.append(_Case(_FILLER_KEX, _FILLER_HOST_KEY, _FILLER_CIPHER, mac, "none"))
    return cases


CASES = _build_cases()


def _case_id(case: _Case) -> str:
    return f"{case.kex}|{case.host_key}|{case.cipher}|{case.mac}|{case.compression}"


class _RecordingServer(asyncssh.SSHServer):
    """Records the server-side connection so the test can read its
    ``recv_*`` extra info once the client has finished authenticating."""

    def __init__(self, sink: list) -> None:
        self._sink = sink

    def connection_made(self, conn) -> None:
        self._sink.append(conn)

    def begin_auth(self, username: str) -> bool:
        return False


@pytest.fixture(autouse=True)
def _install_warning_filters():
    install_asyncssh_warning_filters()


@pytest.mark.asyncio
@pytest.mark.filterwarnings("error")
@pytest.mark.parametrize("case", CASES, ids=_case_id)
async def test_the_handshake_negotiates_the_pinned_algorithm(case: _Case):
    sink: list = []
    listener = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=lambda: _RecordingServer(sink),
        server_host_keys=_HOST_KEYS,
        kex_algs=[case.kex],
        encryption_algs=[case.cipher],
        mac_algs=[case.mac],
        compression_algs=[case.compression],
    )
    try:
        conn = await asyncio.wait_for(
            asyncssh.connect(
                "127.0.0.1",
                port=listener.get_port(),
                username="otto",
                known_hosts=None,
                kex_algs=[case.kex],
                server_host_key_algs=[case.host_key],
                encryption_algs=[case.cipher],
                mac_algs=[case.mac],
                compression_algs=[case.compression],
            ),
            15,
        )
        try:
            # A successful connect with exactly one kex candidate and one
            # host-key candidate on each side IS the proof those two ran:
            # asyncssh records no kex or host-key choice in get_extra_info.
            assert len(sink) == 1
            server_conn = sink[0]
            assert conn.get_extra_info("send_cipher") == case.cipher
            assert server_conn.get_extra_info("recv_cipher") == case.cipher
            assert conn.get_extra_info("send_compression") == case.compression
            assert server_conn.get_extra_info("recv_compression") == case.compression
            if case.cipher in AEAD_CIPHERS:
                # An AEAD cipher's own tag replaces the negotiated MAC.
                assert conn.get_extra_info("send_mac") == case.cipher
                assert server_conn.get_extra_info("recv_mac") == case.cipher
            else:
                assert conn.get_extra_info("send_mac") == case.mac
                assert server_conn.get_extra_info("recv_mac") == case.mac
        finally:
            conn.close()
            await conn.wait_closed()
    finally:
        listener.close()
        await listener.wait_closed()


def test_the_case_builder_covers_every_exercisable_row():
    """The parametrization above is generated, not hand-picked: this guards
    the generator itself, the same way a fixture-derived parametrize needs a
    check that it did not silently shrink."""
    assert CASES, "the case builder produced no cases"
    columns = {
        KEX: exercisable(KEX),
        HOST_KEY: exercisable(HOST_KEY),
        CIPHER: exercisable(CIPHER),
        MAC: exercisable(MAC),
        COMPRESSION: exercisable(COMPRESSION),
    }
    got = {
        KEX: {c.kex for c in CASES},
        HOST_KEY: {c.host_key for c in CASES},
        CIPHER: {c.cipher for c in CASES},
        MAC: {c.mac for c in CASES},
        COMPRESSION: {c.compression for c in CASES},
    }
    for family, expected in columns.items():
        assert got[family] == set(expected), (
            f"{family}: cases cover {got[family]}, expected {set(expected)}"
        )

    for mac in columns[MAC]:
        ciphers = {c.cipher for c in CASES if c.mac == mac}
        assert ciphers - AEAD_CIPHERS, f"{mac} only ever meets an AEAD cipher; its HMAC never runs"
