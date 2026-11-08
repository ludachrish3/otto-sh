"""A test daemon's port is RESERVED, never picked and released (#968).

Nightly 42482952269 failed the exec positive control on one hermetic cell,
once in five repeats, in about three seconds, and its log could not say
which cell or why. The likely shape, reproduced deliberately: the loopback
cell's ``sshd`` used to get its port from ``bind(0)``-and-close, then bind
it later. Linux draws ``bind(0)`` from ``ip_local_port_range``, the same
pool every OUTBOUND connection's source port comes from, so under ``-n4``
any sibling's ``connect()`` could take the port in that gap. Forced, that is
``sshd`` exiting at once with ``Address already in use``: a call-phase
FAILED in well under a second, matching the nightly's timing.

:func:`tests._fixtures.ports.reserved_port` closes it structurally, the way
``otto.tunnel.socat.carrier_port_floor`` closed the same race for tunnel
carriers in #284: a port above the kernel's ephemeral ceiling cannot be
auto-assigned to anyone, and a per-port ``flock`` held for the daemon's
lifetime keeps two workers from choosing the same one.
"""

import getpass
import random
import socket
import time
from pathlib import Path

import pytest

from tests._fixtures.ports import reserved_port
from tests.integration.chaos import _sshd


class _AlwaysLowest(random.Random):
    """An rng that always draws the lowest candidate, so two draws collide on purpose."""

    def choice(self, seq):
        return min(seq)


def _kernel_ephemeral_ceiling() -> int:
    """Read independently of the helper, so the two cannot agree by sharing a bug."""
    low, high = Path("/proc/sys/net/ipv4/ip_local_port_range").read_text().split()
    assert int(low) <= int(high)
    return int(high)


def test_a_reserved_port_is_one_the_kernel_never_hands_out(tmp_path: Path) -> None:
    ceiling = _kernel_ephemeral_ceiling()
    with reserved_port(lock_dir=tmp_path) as port:
        assert ceiling < port <= 65535, (
            f"reserved {port}, inside the kernel's ephemeral range (ceiling {ceiling}): "
            f"any outbound connection could take it before the daemon binds"
        )


def test_two_holders_never_share_a_port(tmp_path: Path) -> None:
    """Both draws aim at the same port; the lock, not luck, must separate them."""
    with (
        reserved_port(lock_dir=tmp_path, rng=_AlwaysLowest()) as first,
        reserved_port(lock_dir=tmp_path, rng=_AlwaysLowest()) as second,
    ):
        assert second != first, "a port still held by one reservation was handed to another"
    # Released on exit: the same draw gets it back.
    with reserved_port(lock_dir=tmp_path, rng=_AlwaysLowest()) as again:
        assert again == first


def test_a_port_another_listener_holds_fails_start_fast_instead_of_hanging(
    tmp_path: Path,
) -> None:
    """Readiness is THIS sshd saying it listens, never "something accepts on the port".

    A bare connect probe is satisfied by whoever holds the port, so start()
    used to return against a foreign listener and the session then hung in
    the ssh handshake (over 120s, measured). sshd itself cannot bind that
    port and exits, so start() must raise, quickly, naming why.
    """
    host_key, _ = _sshd.generate_keypairs(tmp_path / "keys")
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", 0))
        squatter.listen()
        port = squatter.getsockname()[1]
        config = _sshd.write_sshd_config(
            tmp_path / "sshd_config",
            port=port,
            host_key=host_key,
            authorized_keys=tmp_path / "keys" / "authorized_keys",
            user=getpass.getuser(),
        )
        daemon = _sshd.LoopbackSshd(config, tmp_path / "sshd.log")
        started = time.monotonic()
        try:
            with pytest.raises(RuntimeError, match="Address already in use"):
                daemon.start(port)
        finally:
            daemon.stop()
        elapsed = time.monotonic() - started
    # A runaway bound, not a discriminator: the real figure is sshd's start-up.
    assert elapsed < _sshd._READY_TIMEOUT, f"start() took {elapsed:.1f}s to notice"
