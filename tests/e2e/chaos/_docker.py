"""Docker chaos venue construction: pepper (bed) or loopback (hermetic).

Plan 5's docker-chaos scenarios need ONE docker-capable SSH parent host per
venue, selected by ``OTTO_CHAOS_DOCKER`` (unset/``"pepper"`` = bed, or
``"loopback"``):

- ``pepper``: the shared bed daemon on ``10.10.200.13``, same constructor
  ``tests/integration/test_docker_compose.py``'s ``parent`` fixture uses —
  reused rather than duplicated so the two suites can never drift apart on
  what "the pepper docker parent" means.
- ``loopback``: this runner's OWN docker daemon, wrapped by tier-2's
  throwaway sshd (``tests/integration/chaos/_sshd.py``) so CI (no bed route)
  can still certify the harness construction end to end. Never touches the
  bed.

Nothing here opens a connection eagerly — ``UnixHost`` construction is inert
until the first ``exec``/``run``/``put``/``get`` (see
``UnixHost.__post_init__``), so building one of these is cheap and safe to
repeat per probe.
"""

import contextlib
import getpass
import uuid
from collections.abc import Iterator
from pathlib import Path

from otto.host.login_proxy import Cred
from otto.host.options import SshOptions
from otto.host.unix_host import UnixHost
from tests._ambient_env import ambient
from tests.integration.chaos._sshd import (
    LoopbackSshd,
    free_port,
    generate_keypairs,
    write_sshd_config,
)


def docker_venue() -> str:
    """Which docker chaos venue to target: ``"pepper"`` (default) or ``"loopback"``."""
    return ambient("OTTO_CHAOS_DOCKER", "pepper")


def pepper_parent() -> UnixHost:
    """The bed docker parent: pepper (10.10.200.13).

    Exact shape of ``tests/integration/test_docker_compose.py``'s ``parent``
    fixture — same ip/creds/board/term/transfer/docker_capable — so pepper
    means the same thing to both suites.
    """
    return UnixHost(
        ip="10.10.200.13",
        element="pepper",
        creds=[Cred(login="vagrant", password="vagrant")],
        board="seed",
        is_virtual=True,
        term="ssh",
        transfer="scp",
        docker_capable=True,
    )


@contextlib.contextmanager
def loopback_parent(work_dir: Path) -> Iterator[UnixHost]:
    """Hermetic docker parent: this runner's own daemon via a throwaway sshd.

    Mirrors ``tests/integration/chaos/conftest.py``'s ``chaos_target`` setup
    (keypairs -> sshd config -> ``LoopbackSshd`` on a free loopback port),
    but wraps THIS host's docker daemon rather than generating a lab/SUT
    pair — the caller already has a ``Repo``/``Lab`` of its own. Stops the
    sshd on exit no matter what happened inside the ``with`` block.
    """
    host_key, client_key = generate_keypairs(work_dir / "keys")
    port = free_port()
    cfg = write_sshd_config(
        work_dir / "sshd_config",
        port=port,
        host_key=host_key,
        authorized_keys=work_dir / "keys" / "authorized_keys",
        user=getpass.getuser(),
    )
    sshd = LoopbackSshd(cfg, work_dir / "sshd.log")
    sshd.start(port)
    try:
        yield UnixHost(
            ip="127.0.0.1",
            element="loopback",
            creds=[Cred(login=getpass.getuser(), password="unused-pubkey-auth")],
            board="seed",
            is_virtual=True,
            term="ssh",
            transfer="sftp",
            docker_capable=True,
            ssh_options=SshOptions(port=port, client_keys=[str(client_key)]),
        )
    finally:
        sshd.stop()


def fresh_project() -> str:
    """A unique compose project name for one scenario's stack.

    Contains ``-e2e-`` so any staging directories or docker resources that
    survive a bug are reaper-coverable (matches the ``-e2e-`` convention the
    rest of the e2e docker suites use for the same reason).
    """
    return f"otto-repo1-e2e-{uuid.uuid4().hex[:8]}"
