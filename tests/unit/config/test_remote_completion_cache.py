"""Unit tests for the remote-path completion sidecar cache."""

from datetime import datetime, timedelta, timezone

import pytest

from otto.reservations import ReservationWindow

NOW = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def cache_file(tmp_path, monkeypatch):
    """Point both cache modules' path resolution at tmp_path."""
    main = tmp_path / ".otto" / "completion_cache.json"
    main.parent.mkdir(parents=True)
    monkeypatch.setattr("otto.config.completion_cache._cache_path", lambda: main)
    return main.with_name("remote_completion_cache.json")


def test_listing_roundtrip_within_ttl(cache_file):
    from otto.config import remote_completion_cache as rcc

    entries = [
        rcc.ListingEntry(name="logs", is_dir=True),
        rcc.ListingEntry(name="a.txt", is_dir=False),
    ]
    rcc.store_listing("dut1", "/var", entries, NOW)
    assert rcc.cached_listing("dut1", "/var", NOW + timedelta(seconds=44)) == entries


def test_listing_expires_after_ttl(cache_file):
    from otto.config import remote_completion_cache as rcc

    rcc.store_listing("dut1", "/var", [rcc.ListingEntry(name="x", is_dir=False)], NOW)
    assert rcc.cached_listing("dut1", "/var", NOW + timedelta(seconds=46)) is None


def test_listing_cap_evicts_oldest(cache_file):
    from otto.config import remote_completion_cache as rcc

    for i in range(rcc.MAX_DIRS_PER_HOST + 1):
        rcc.store_listing("dut1", f"/d{i}", [], NOW + timedelta(seconds=i * 0.001))
    assert rcc.cached_listing("dut1", "/d0", NOW + timedelta(seconds=1)) is None
    assert (
        rcc.cached_listing("dut1", f"/d{rcc.MAX_DIRS_PER_HOST}", NOW + timedelta(seconds=1))
        is not None
    )


def test_corrupt_cache_treated_as_empty(cache_file):
    from otto.config import remote_completion_cache as rcc

    cache_file.write_text("{not json")
    assert rcc.cached_listing("dut1", "/var", NOW) is None
    rcc.store_listing("dut1", "/var", [], NOW)  # rewrites without raising
    assert rcc.cached_listing("dut1", "/var", NOW) == []


@pytest.mark.parametrize(
    "blob",
    [
        pytest.param('{"schema": 999, "listings": {"dut1": {"/var": {}}}}', id="future-schema"),
        pytest.param('["not", "a", "mapping"]', id="wrong-toplevel-type"),
        pytest.param('{"schema": 1, "listings": "nonsense"}', id="wrong-section-type"),
    ],
)
def test_foreign_shape_treated_as_empty(cache_file, blob):
    """Valid JSON otto did not write reads as empty and is rewritten, never crashes."""
    from otto.config import remote_completion_cache as rcc

    cache_file.write_text(blob)
    assert rcc.cached_listing("dut1", "/var", NOW) is None
    rcc.store_listing("dut1", "/var", [rcc.ListingEntry(name="x", is_dir=True)], NOW)
    assert rcc.cached_listing("dut1", "/var", NOW) == [rcc.ListingEntry(name="x", is_dir=True)]


def _win(resource, start, end):
    return ReservationWindow(resource=resource, start=start, end=end)


def test_reservation_windows_pass_and_block_boundary(cache_file):
    from otto.config import remote_completion_cache as rcc

    end = NOW + timedelta(minutes=30)
    rcc.store_reservation_windows("alice", [_win("r1", NOW - timedelta(hours=1), end)], NOW)
    # Within the 120 s block and covered by the window -> True
    assert rcc.cached_reservation_ok("alice", {"r1"}, NOW + timedelta(seconds=60)) is True
    # Past the 120 s block -> stale, needs refresh
    assert rcc.cached_reservation_ok("alice", {"r1"}, NOW + timedelta(seconds=121)) is None


def test_reservation_edge_invalidates_before_block(cache_file):
    from otto.config import remote_completion_cache as rcc

    end = NOW + timedelta(seconds=30)  # edge INSIDE the 120 s block
    rcc.store_reservation_windows("alice", [_win("r1", NOW - timedelta(hours=1), end)], NOW)
    assert rcc.cached_reservation_ok("alice", {"r1"}, NOW + timedelta(seconds=29)) is True
    # One second past the edge: entry must be invalid (None), NOT a cached False —
    # the whole point is forcing a live refresh at the boundary.
    assert rcc.cached_reservation_ok("alice", {"r1"}, NOW + timedelta(seconds=31)) is None


def test_reservation_future_start_edge_also_clamps(cache_file):
    from otto.config import remote_completion_cache as rcc

    start = NOW + timedelta(seconds=40)
    rcc.store_reservation_windows("alice", [_win("r1", start, start + timedelta(hours=4))], NOW)
    # Booking not started yet -> gate refuses, from cache
    assert rcc.cached_reservation_ok("alice", {"r1"}, NOW + timedelta(seconds=10)) is False
    # Past the start edge -> cache invalid, force refresh
    assert rcc.cached_reservation_ok("alice", {"r1"}, NOW + timedelta(seconds=41)) is None


def test_reservation_missing_resource_is_false(cache_file):
    from otto.config import remote_completion_cache as rcc

    rcc.store_reservation_windows(
        "alice", [_win("r1", NOW - timedelta(hours=1), NOW + timedelta(hours=1))], NOW
    )
    assert rcc.cached_reservation_ok("alice", {"r1", "r2"}, NOW + timedelta(seconds=1)) is False


def test_reservation_flat_set_fallback(cache_file):
    from otto.config import remote_completion_cache as rcc

    rcc.store_reservation_set("alice", {"r1", "r2"}, NOW)
    assert rcc.cached_reservation_ok("alice", {"r1"}, NOW + timedelta(seconds=119)) is True
    assert rcc.cached_reservation_ok("alice", {"r1"}, NOW + timedelta(seconds=121)) is None


def test_naive_window_bounds_degrade_instead_of_raising(cache_file):
    """A backend handing back naive datetimes must not traceback into the shell.

    ``ReservationWindow`` requires aware bounds, but a third-party backend
    returning a bare ``datetime.now()`` would otherwise blow up on the first
    comparison. It degrades to a miss (and the flat TTL) instead.
    """
    from otto.config import remote_completion_cache as rcc

    naive_start = datetime(2026, 8, 6, 11, 0, 0)  # noqa: DTZ001 — the hazard under test
    naive_end = datetime(2026, 8, 6, 13, 0, 0)  # noqa: DTZ001 — the hazard under test
    rcc.store_reservation_windows("alice", [_win("r1", naive_start, naive_end)], NOW)

    # No clamp from an uncomparable edge: the entry keeps the flat 120 s block…
    assert rcc.cached_reservation_ok("alice", {"r1"}, NOW + timedelta(seconds=121)) is None
    # …and the unusable window simply covers nothing, rather than raising.
    assert rcc.cached_reservation_ok("alice", {"r1"}, NOW + timedelta(seconds=1)) is False


NAIVE_NOW = datetime(2026, 8, 6, 12, 0, 0)  # noqa: DTZ001 — the hazard under test


@pytest.fixture
def aware_cache(cache_file):
    """A cache written entirely by an ordinary AWARE caller.

    Load-bearing starting state: a naive ``now`` against a cache that is *also*
    naive never mixes the two, so a test starting from an empty cache and
    writing with a naive clock proves nothing. Every timestamp on disk here is
    aware, which is what a naive reader would have to compare itself against.
    """
    from otto.config import remote_completion_cache as rcc

    rcc.store_listing("dut1", "/var", [rcc.ListingEntry(name="x", is_dir=True)], NOW)
    rcc.store_reservation_windows(
        "alice", [_win("r1", NOW - timedelta(hours=1), NOW + timedelta(hours=1))], NOW
    )
    assert cache_file.exists()
    return cache_file


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda rcc: rcc.cached_listing("dut1", "/var", NAIVE_NOW), id="cached_listing"
        ),
        pytest.param(
            lambda rcc: rcc.cached_reservation_ok("alice", {"r1"}, NAIVE_NOW),
            id="cached_reservation_ok",
        ),
        pytest.param(
            lambda rcc: rcc.store_listing("dut1", "/tmp", [], NAIVE_NOW), id="store_listing"
        ),
        pytest.param(
            lambda rcc: rcc.store_reservation_set("alice", {"r1"}, NAIVE_NOW),
            id="store_reservation_set",
        ),
        pytest.param(
            lambda rcc: rcc.store_reservation_windows(
                "alice", [_win("r1", NOW, NOW + timedelta(hours=1))], NAIVE_NOW
            ),
            id="store_reservation_windows",
        ),
    ],
)
def test_naive_now_against_an_aware_cache_never_raises(aware_cache, call):
    """Every public entry point refuses a naive clock instead of raising TypeError.

    The mixed comparison — naive ``now``, aware stored timestamp — is the case
    that would traceback into the user's shell mid-TAB. `_parse`'s aware-only
    rejection guards the STORED side only; nothing there filters the caller's
    clock, so each entry point is exercised on its own.
    """
    from otto.config import remote_completion_cache as rcc

    call(rcc)  # must not raise


def test_naive_now_reads_as_a_miss_and_writes_nothing(aware_cache):
    """The refusal degrades to a miss, and no naive timestamp reaches the file."""
    from otto.config import remote_completion_cache as rcc

    before = aware_cache.read_text()
    assert rcc.cached_listing("dut1", "/var", NAIVE_NOW) is None
    assert rcc.cached_reservation_ok("alice", {"r1"}, NAIVE_NOW) is None
    rcc.store_listing("dut1", "/tmp", [rcc.ListingEntry(name="y", is_dir=False)], NAIVE_NOW)
    rcc.store_reservation_set("alice", {"r9"}, NAIVE_NOW)
    assert aware_cache.read_text() == before, "a naive clock wrote to the cache"
    # The aware caller's own entries are untouched and still served.
    assert rcc.cached_listing("dut1", "/var", NOW) == [rcc.ListingEntry(name="x", is_dir=True)]
    assert rcc.cached_reservation_ok("alice", {"r1"}, NOW) is True


def test_unknown_user_is_none(cache_file):
    from otto.config import remote_completion_cache as rcc

    assert rcc.cached_reservation_ok("nobody", {"r1"}, NOW) is None


def test_clear_removes_file(cache_file):
    from otto.config import remote_completion_cache as rcc

    rcc.store_listing("dut1", "/var", [], NOW)
    assert cache_file.exists()
    assert rcc.clear_remote_cache() is True
    assert not cache_file.exists()
    assert rcc.clear_remote_cache() is False


def test_caching_disabled_is_inert(tmp_path, monkeypatch):
    """With ``_cache_path`` -> None every entry point no-ops.

    DEFENSIVE, not reachable: the cache moved to the workspace home, which
    is derived from ``OTTO_SUT_DIRS`` alone, so ``_cache_path`` no longer
    has a "caching disabled" case and nothing in production returns None.
    The None is forced here because the branch still exists and the callers
    below still guard on it -- if that guard is ever removed, this is the
    test that should be deleted with it rather than quietly left passing.
    """
    monkeypatch.setattr("otto.config.completion_cache._cache_path", lambda: None)
    from otto.config import remote_completion_cache as rcc

    rcc.store_listing("dut1", "/var", [rcc.ListingEntry(name="x", is_dir=True)], NOW)
    assert rcc.cached_listing("dut1", "/var", NOW) is None
    rcc.store_reservation_set("alice", {"r1"}, NOW)
    assert rcc.cached_reservation_ok("alice", {"r1"}, NOW) is None
    assert rcc.clear_remote_cache() is False
    assert not list(tmp_path.rglob("*.json"))
