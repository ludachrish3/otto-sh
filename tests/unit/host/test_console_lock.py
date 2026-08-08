"""Unit tests for the writer-fair console lock (lab-free, multiprocessing)."""

from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

import pytest

from otto.utils import wait_for
from tests._fixtures._console_lock import console_access


def _reader_hold_then_barrier(lock_dir: str, barrier) -> None:
    # Hold a SHARED lock and wait for the peer reader to also be inside it.
    with console_access(Path(lock_dir), exclusive=False):
        barrier.wait(timeout=5)


def _reader_churn(lock_dir: str, stop, cycles) -> None:
    # Continuously take/release SHARED locks to pressure an EXCLUSIVE waiter.
    # `cycles` is the premise counter: each increment happens INSIDE a held
    # SHARED lock, so a nonzero count proves real reader pressure existed —
    # without it, four children crashing at import would leave the exclusive
    # acquisition uncontended and the starvation assert vacuously green.
    while not stop.is_set():
        with console_access(Path(lock_dir), exclusive=False):
            with cycles.get_lock():
                cycles.value += 1
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


@pytest.mark.serial_timing
def test_writer_not_starved_by_reader_churn(tmp_path):
    stop = mp.Event()
    cycles = mp.Value("i", 0)
    readers = [
        mp.Process(target=_reader_churn, args=(str(tmp_path), stop, cycles)) for _ in range(4)
    ]
    for r in readers:
        r.start()
    try:
        # Premise control replacing the old blind `sleep(0.3)` ramp. NB the
        # aggregate counter does NOT prove one cycle PER child (one survivor
        # can supply all 4); a partial crash is caught by the exitcode assert
        # below instead. Expiry names the failed premise.
        wait_for(
            lambda: cycles.value >= 4,
            timeout=10.0,
            on_timeout=lambda: (
                f"reader churn never ramped: {cycles.value} cycles; "
                f"reader exitcodes={[r.exitcode for r in readers]}"
            ),
        )
        start = time.monotonic()
        with console_access(tmp_path, exclusive=True):
            waited = time.monotonic() - start
        assert waited < 5.0, f"exclusive waiter starved: waited {waited:.2f}s"
        # The churn survived the exclusive round-trip (readers still cycling,
        # not crashed) — the starvation number above was measured under load.
        assert all(r.exitcode is None for r in readers), (
            f"reader(s) died mid-test: exitcodes={[r.exitcode for r in readers]}"
        )
    finally:
        stop.set()
        for r in readers:
            r.join(timeout=5)
