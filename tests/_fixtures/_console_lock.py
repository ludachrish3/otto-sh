"""Writer-fair cross-worker lock serializing access to single-client consoles.

Per-device embedded tests take a SHARED lock (they touch only their own console;
different devices parallelize across xdist workers); the fan-out / contention
tests take an EXCLUSIVE lock (they open every console, or two clients to one).
Plain ``flock`` is reader-preferring on Linux, so a steady stream of SHARED
holders starves the EXCLUSIVE waiter (confirmed live — see
docs/superpowers/specs/2026-06-06-embedded-console-lock-fairness-design.md).

This is a turnstile-gated reader/writer lock: every caller passes through a
*gate* mutex before taking the *resource* lock. A waiting writer holds the gate,
so new readers block at the gate while in-flight readers drain — the writer
can't be starved. Readers drop the gate the instant they hold the SHARED
resource lock, so they still run concurrently.
"""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

GATE_NAME = "zephyr_console.gate"
RESOURCE_NAME = "zephyr_console.resource"


@contextmanager
def console_access(lock_dir: Path, *, exclusive: bool) -> Iterator[None]:
    """Acquire the fair console lock — SHARED (``exclusive=False``) or EXCLUSIVE.

    ``lock_dir`` must be common to every xdist worker (use
    ``tmp_path_factory.getbasetemp().parent``). Closing the fds in ``finally``
    releases the locks even if an explicit unlock is skipped — e.g. a
    pytest-timeout signal interrupts the holder.
    """
    gate_fd = os.open(str(lock_dir / GATE_NAME), os.O_RDWR | os.O_CREAT, 0o644)
    resource_fd = os.open(str(lock_dir / RESOURCE_NAME), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(gate_fd, fcntl.LOCK_EX)  # enter the turnstile
        if exclusive:
            # Hold the gate while waiting for the resource: new readers block at
            # the gate, in-flight readers drain, then the writer acquires. The
            # gate is released by the outer finally (after the resource).
            fcntl.flock(resource_fd, fcntl.LOCK_EX)
        else:
            fcntl.flock(resource_fd, fcntl.LOCK_SH)
            fcntl.flock(gate_fd, fcntl.LOCK_UN)  # let the next caller enter
        yield
    finally:
        # Closing each fd releases any lock held on it (the flock is tied to the
        # open file description), so this is correct even if a flock above was
        # interrupted. Resource first, then gate.
        os.close(resource_fd)
        os.close(gate_fd)
