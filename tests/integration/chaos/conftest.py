"""Session fixtures for the tier-2 chaos suite (chaos spec, Tier 2).

Default target: a throwaway loopback sshd owned by this session — hermetic
on the dev VM and ubuntu-latest alike. ``OTTO_CHAOS_BED_HOST`` (lab leg
only) redirects the otto subprocess at a unix bed host; signals still
only ever go to the local otto process. Host-down in bed mode fails LOUD
with the host's name — never a skip (dev-VM rule).
"""

import getpass
import socket
from collections.abc import Iterator

import pytest

from tests._fixtures.fd_watermark import (
    _fd_watermark,  # noqa: F401 — imported fixture, registered by name
)

from ._sshd import LoopbackSshd, free_port, generate_keypairs, write_sshd_config
from ._target import ChaosTarget, bed_host_override, make_bed_target, make_loopback_target


@pytest.fixture(scope="session")
def chaos_target(tmp_path_factory: pytest.TempPathFactory) -> "Iterator[ChaosTarget]":
    bed = bed_host_override()
    if bed is not None:
        target = make_bed_target(bed)
        try:
            with socket.create_connection((target.ssh_host, target.ssh_port), timeout=5):
                pass
        except OSError as e:
            raise RuntimeError(
                f"OTTO_CHAOS_BED_HOST={bed}: {target.ssh_host}:{target.ssh_port} "
                f"unreachable — bed down?"
            ) from e
        yield target
        return

    root = tmp_path_factory.mktemp("chaos")
    host_key, client_key = generate_keypairs(root / "keys")
    port = free_port()
    cfg = write_sshd_config(
        root / "sshd_config",
        port=port,
        host_key=host_key,
        authorized_keys=root / "keys" / "authorized_keys",
        user=getpass.getuser(),
    )
    sshd = LoopbackSshd(cfg, root / "sshd.log")
    sshd.start(port)
    try:
        yield make_loopback_target(root, port=port, client_key=client_key)
    finally:
        sshd.stop()
