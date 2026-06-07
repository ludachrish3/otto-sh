"""Unit tests for the writer-fair console lock (lab-free, multiprocessing)."""
from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

from tests.integration.host._console_lock import console_access


def _reader_hold_then_barrier(lock_dir: str, barrier) -> None:
    # Hold a SHARED lock and wait for the peer reader to also be inside it.
    with console_access(Path(lock_dir), exclusive=False):
        barrier.wait(timeout=5)


def _reader_churn(lock_dir: str, stop) -> None:
    # Continuously take/release SHARED locks to pressure an EXCLUSIVE waiter.
    while not stop.is_set():
        with console_access(Path(lock_dir), exclusive=False):
            time.sleep(0.02)
        time.sleep(0.005)


def test_two_readers_hold_shared_concurrently(tmp_path):
    # If the lock wrongly serialized readers, the Barrier(2) would time out and
    # the children would exit non-zero.
    barrier = mp.Barrier(2)
    ps = [
        mp.Process(target=_reader_hold_then_barrier, args=(str(tmp_path), barrier))
        for _ in range(2)
    ]
    for p in ps:
        p.start()
    for p in ps:
        p.join(timeout=15)
    assert all(p.exitcode == 0 for p in ps), "readers did not hold SHARED concurrently"


def test_writer_not_starved_by_reader_churn(tmp_path):
    stop = mp.Event()
    readers = [
        mp.Process(target=_reader_churn, args=(str(tmp_path), stop))
        for _ in range(4)
    ]
    for r in readers:
        r.start()
    try:
        time.sleep(0.3)  # let the churn ramp up
        start = time.monotonic()
        with console_access(tmp_path, exclusive=True):
            waited = time.monotonic() - start
        assert waited < 5.0, f"exclusive waiter starved: waited {waited:.2f}s"
    finally:
        stop.set()
        for r in readers:
            r.join(timeout=5)
