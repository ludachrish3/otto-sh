"""Lease of one free Unix host from a small pool, across every session of this user.

A transfer/command test that needs *a* Unix host (not a specific one) leases
whichever pool member is free, spreading load off the historical favourite
(test1). Built on the same fd-flock idiom as ``_console_lock``: a non-blocking
``LOCK_EX`` on a per-host lock file claims it; closing the fd in ``finally``
releases it even if a pytest-timeout signal interrupts the holder.

A flock belongs to an open file description, so a process that asks again
for a host it already leases would wait on itself forever. The process keeps
a record of what it holds and refuses such a lease at once, naming the
holder.
"""

from __future__ import annotations

import fcntl
import os
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

from tests._fixtures.bed_locks import lock_dir_or_root

# The unix-lab Unix peers with identical transfer backends. test3 is leased
# directly (no test1 hop — see the Phase-1 lab-data simplification).
UNIX_POOL: tuple[str, ...] = ("test1", "test2", "test3")

DOCKER_HOST = "test3"
"""The lab host whose docker daemon the suite's ``-e2e-`` compose stacks run on."""

_POLL_SECONDS = 0.05

# What this process leases now: lock file -> who took it.
_HELD: dict[Path, str] = {}


def _caller() -> str:
    """The first frame outside this module and ``contextlib``: who is taking the lease."""
    frame = sys._getframe(1)
    while frame.f_code.co_filename in (__file__, contextmanager.__code__.co_filename):
        frame = frame.f_back
        if frame is None:
            return "an unknown caller"
    return f"{frame.f_code.co_name} ({frame.f_code.co_filename}:{frame.f_lineno})"


class SelfLeaseError(RuntimeError):
    """A lease asked only for hosts this process already holds: it would wait on itself."""


@contextmanager
def lease_unix_host(
    candidates: Sequence[str] = UNIX_POOL, *, lock_dir: Path | None = None
) -> Iterator[str]:
    """Lease the first free host in ``candidates``; yield its element name.

    The lock files live under :func:`~tests._fixtures.bed_locks.bed_lock_root`;
    ``lock_dir`` replaces it only for a test of this mechanism. Polls until a
    host another process holds is free; holds an exclusive flock for the lease,
    released by closing the fd in ``finally`` — correct even if a
    pytest-timeout signal interrupts the holder. A candidate this process
    already holds is skipped, and :class:`SelfLeaseError` is raised at once
    when every candidate is one.
    """
    root = lock_dir_or_root(lock_dir)
    paths = {element: root / f"unix_pool.{element}" for element in candidates}
    mine = {element: _HELD[path] for element, path in paths.items() if path in _HELD}
    if mine and len(mine) == len(paths):
        held = ", ".join(f"{element} by {holder}" for element, holder in mine.items())
        raise SelfLeaseError(
            f"{_caller()} asked for a lease on {list(candidates)}, but this process already "
            f"holds every one of them ({held}); waiting would never end"
        )
    while True:
        for element, path in paths.items():
            if element in mine:
                continue
            fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)  # busy — try the next candidate
                continue
            _HELD[path] = _caller()
            try:
                yield element
                return
            finally:
                del _HELD[path]
                os.close(fd)  # releases the flock
        time.sleep(_POLL_SECONDS)  # all busy — back off and retry


@contextmanager
def docker_host_lease() -> Iterator[str]:
    """Lease :data:`DOCKER_HOST` against every session of this user; yield its element.

    Every ``-e2e-`` stack on the docker host lives under this lock for its
    whole life, and ``tests/integration/conftest.py``'s orphan reaper takes it
    too, so the reap waits for a live stack instead of removing it.
    """
    with lease_unix_host([DOCKER_HOST]) as element:
        yield element
