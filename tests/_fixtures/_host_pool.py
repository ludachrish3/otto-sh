"""Cross-worker lease of one free Unix host from a small pool.

A transfer/command test that needs *a* Unix host (not a specific one) leases
whichever pool member is free, spreading load off the historical favourite
(carrot). Built on the same fd-flock idiom as ``_console_lock``: a non-blocking
``LOCK_EX`` on a per-host lock file claims it; closing the fd in ``finally``
releases it even if a pytest-timeout signal interrupts the holder.
"""

from __future__ import annotations

import fcntl
import os
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

# The veggies-lab Unix peers with identical transfer backends. Pepper is leased
# directly (no carrot hop — see the Phase-1 lab-data simplification).
UNIX_POOL: tuple[str, ...] = ("carrot", "tomato", "pepper")

_POLL_SECONDS = 0.05


@contextmanager
def lease_unix_host(lock_dir: Path, candidates: Sequence[str] = UNIX_POOL) -> Iterator[str]:
    """Lease the first free host in ``candidates``; yield its element name.

    ``lock_dir`` must be common to every xdist worker (use
    ``tmp_path_factory.getbasetemp().parent``). Polls until a host is free;
    holds an exclusive flock for the lease, released by closing the fd in
    ``finally`` — correct even if a pytest-timeout signal interrupts the holder.
    """
    while True:
        for element in candidates:
            fd = os.open(
                str(lock_dir / f"unix_pool.{element}"),
                os.O_RDWR | os.O_CREAT,
                0o644,
            )
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)  # busy — try the next candidate
                continue
            try:
                yield element
                return
            finally:
                os.close(fd)  # releases the flock
        time.sleep(_POLL_SECONDS)  # all busy — back off and retry
