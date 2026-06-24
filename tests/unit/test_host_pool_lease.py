"""Cross-worker host-pool lease mechanics (no VM — flock over a tmp dir)."""
from __future__ import annotations

import multiprocessing as mp
import time
from pathlib import Path

from tests._fixtures._host_pool import lease_unix_host, UNIX_POOL


def test_lease_yields_a_pool_member(tmp_path: Path) -> None:
    with lease_unix_host(tmp_path) as element:
        assert element in UNIX_POOL


def test_two_leases_pick_distinct_hosts(tmp_path: Path) -> None:
    with lease_unix_host(tmp_path) as a:
        with lease_unix_host(tmp_path) as b:
            assert a != b  # second lease skips the busy first host


def test_lease_releases_on_exit(tmp_path: Path) -> None:
    with lease_unix_host(tmp_path, candidates=["carrot"]) as a:
        assert a == "carrot"
    # carrot is free again — re-leasing the single-host pool succeeds immediately
    start = time.monotonic()
    with lease_unix_host(tmp_path, candidates=["carrot"]) as b:
        assert b == "carrot"
    assert time.monotonic() - start < 1.0


def _hold(lock_dir: str, secs: float, q) -> None:
    with lease_unix_host(Path(lock_dir), candidates=["carrot"]):
        q.put("held")
        time.sleep(secs)


def test_lease_is_cross_process(tmp_path: Path) -> None:
    """A second process blocks until the first releases (cross-worker safety)."""
    q = mp.Queue()
    p = mp.Process(target=_hold, args=(str(tmp_path), 0.5, q))
    p.start()
    assert q.get(timeout=5) == "held"
    start = time.monotonic()
    with lease_unix_host(tmp_path, candidates=["carrot"]):
        waited = time.monotonic() - start
    p.join()
    assert waited >= 0.3  # had to wait for the other process to release
