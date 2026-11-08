"""Loopback ports for test daemons: RESERVED for the daemon's lifetime, never raced.

The pattern this replaces -- ``bind(("127.0.0.1", 0))``, read the port, close
the socket, hand the number to a daemon that binds it later -- loses that
port in the gap whenever anything else wants one. On Linux ``bind(0)`` draws
from ``ip_local_port_range``, which is also where every OUTBOUND connection's
source port comes from, so under ``-n auto`` the thief is any sibling test's
``connect()``. That is the likely shape of #968 (nightly 42482952269: one
hermetic positive control red in about three seconds, once in five repeats
of the suite): forced deliberately, it is ``sshd`` exiting at once with
``Address already in use``. The nightly log could not say which cell failed
or why, so this is the reproduced mechanism, not a recorded cause.

:func:`reserved_port` closes both halves of that gap structurally rather than
narrowing it:

- **The kernel.** Ports are drawn ABOVE the kernel's ephemeral ceiling, so
  no ``connect()`` is ever auto-assigned one. Which ports clear the ceiling is
  not decided here: it is ``otto.tunnel.socat``'s decision for tunnel
  carriers, made for the same race (#284), and this calls it --
  :data:`~otto.tunnel.socat.FREE_PORT_PROBE_COMMAND` run on this machine,
  :func:`~otto.tunnel.socat.parse_listening_ports` for the listeners to avoid,
  :func:`~otto.tunnel.socat.carrier_port_floor` over
  :func:`~otto.tunnel.socat.parse_ephemeral_ceiling` for the floor, and
  :func:`~otto.tunnel.socat.pick_random_free_port` for the draw.
- **Each other.** Every reservation holds an exclusive ``flock`` on
  ``port-<n>.lock`` under :func:`~tests._fixtures.bed_locks.bed_lock_root`,
  the one directory every session and worker of this user meets in, until the
  ``with`` block ends. A draw whose lock is held is struck off and redrawn.

What remains is a process that binds a port up there BY NUMBER without taking
the lock -- otto's own tunnel carriers do, drawing from the same range above
the ceiling, so a tunnel e2e lane beside a daemon test can still meet one.
The probe's listener set keeps an already-listening port out of the draw,
and a daemon that loses its bind regardless fails at once and says so
(``LoopbackSshd`` reads its readiness off ``sshd``'s own log) rather than
hanging.

Where the kernel leaves too little room above its ceiling,
``carrier_port_floor`` falls back to ``49152``, which overlaps a default
ephemeral range. The lock still separates this tree's own daemons; only the
outbound-connection half reopens there, and the daemon's own start-up reports
a lost bind loudly rather than hanging (``LoopbackSshd`` reads its readiness
off ``sshd``'s own log).
"""

import contextlib
import fcntl
import os
import random
import subprocess
from collections.abc import Iterator
from pathlib import Path

from otto.tunnel.socat import (
    FREE_PORT_PROBE_COMMAND,
    carrier_port_floor,
    parse_ephemeral_ceiling,
    parse_listening_ports,
    pick_random_free_port,
)
from tests._fixtures.bed_locks import lock_dir_or_root

#: ``SystemRandom``, not the ``random`` module: pytest-randomly reseeds the
#: module generator identically in every xdist worker, which would line every
#: worker's first draw up on the same port. The lock would still separate
#: them, but by contention on every reservation instead of by exception.
_RNG = random.SystemRandom()


@contextlib.contextmanager
def reserved_port(
    *, lock_dir: "Path | None" = None, rng: "random.Random | None" = None
) -> Iterator[int]:
    """Yield a loopback port that stays this caller's until the block exits.

    Bind the daemon INSIDE the block. *lock_dir* replaces the shared lock root
    and *rng* the draw, both only for a test of this mechanism.
    """
    root = lock_dir_or_root(lock_dir)
    draw = _RNG if rng is None else rng
    probe = subprocess.run(
        ["sh", "-c", FREE_PORT_PROBE_COMMAND],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    used = parse_listening_ports(probe)
    ceiling = parse_ephemeral_ceiling(probe)
    floor = carrier_port_floor([] if ceiling is None else [ceiling])
    while True:
        port = pick_random_free_port(used, draw, lo=floor)
        fd = os.open(str(root / f"port-{port}.lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            used.add(port)
            continue
        try:
            yield port
        finally:
            # Closing the fd drops the flock with it, even if the block was
            # interrupted mid-way (a pytest-timeout signal, say).
            os.close(fd)
        return
